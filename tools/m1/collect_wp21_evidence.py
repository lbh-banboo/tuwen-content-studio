#!/usr/bin/env python
"""WP21 证据采集脚本。

产出的文件（落在 `evidence/m1/`）：

| 文件 | 内容 |
|---|---|
| `wp21-embedding.log` | 2 张表结构 / 约束 / 1 个触发器 / 迁移版本，+ 活性验证清单 |

## 活性验证清单

（条数**不写在这里** —— 它随后续开发生长，写死就注定过期且不报错，
ISS-78。条数由内联脚本的 `RESULT` 行现场派生，外层用 `EXPECTED_CHECKS` 对账。）

结构核对只能证明约束**存在**，不能证明它**生效**。本包验收原文
（研发计划 04 §4.3「**近似品牌案例不串事实**」）的后半句是一条安全约束 ——
安全约束的特征是"做不到不报错，只是有一个口子一直开着"。
因此每一条防线都必须**拿本该被拦的输入去试写**。

1. **无维度列真的无维度**：`atttypmod = -1` + 类型是 `vector`。
   这是"多维度 profile 共存"的前提（pgvector 硬约束：无维度列建不了
   HNSW，因此索引改用每 profile 部分索引）。
2. **HNSW 部分索引真的能建在表达式上**：建两个**不同维度**的 profile
   （1024 / 512）→ 各建一个部分索引 → 断言两个索引**共存**、
   每个 `indexdef` 都含 ` WHERE ` 与 `hnsw`。
   这是本包最要紧的一处设计判断的全部证据。
3. **查询计划命中本 profile 的索引**：`EXPLAIN` 输出里出现
   **该 profile 的索引名**（不是笼统断言 "Index Scan" ——
   那会放过"命中了错的索引"，判据纪律第 3 条）。
   ⚠️ **这条判据修过一次，前因必须记住**（ISS 已登记）：第一版只写了
   2 行向量、并靠 `enable_seqscan=off` 压计划，结果实测 **FAIL**。
   根因**不是实现缺陷**，而是判据本身不成立 —— 精确复现（真表、
   自然优化器、B-tree 在场）测得的阈值：

   ```
      行数    自然优化器                     未 ANALYZE
        3     未命中（未 ANALYZE 时偶然命中）
       50     未命中                        未命中
      200     未命中                        未命中
      500     未命中                        未命中
     1000     命中                          命中
   ```

   3 行时 HNSW（8.02）与 B-tree（8.15）是**成本平局**，平局由统计
   新鲜度决定 → 该判据区分的是"统计状态"而不是"实现对不对"
   （判据纪律第 7 条：判据必须能区分两种实现）。
   因此改为：**造 2000 行向量**（`BULK_ROWS`，留一倍余量）
   + **自然优化器**（不再用 `enable_seqscan=off` 削弱条件 ——
   真实优化器不靠它，靠它压出来的计划证明不了"索引真的会被用"）。
   同时补一条**负判据**：把 `ORDER BY` 里的 `::vector(1024)` 去掉后
   必须**用不上**该索引 —— 没有它，正判据即使恒真也无人发现。
4. **混用守卫正向**：维度与 `model_version` 都对的写入**必须能成功**
   （一个"见谁都拒"的守卫也能让负向全绿）。
5. **混用守卫负向 A（异维度）**：512 维写进 1024 维的 profile → `23514`。
6. **混用守卫负向 B（同维度、异 model_version）** —— **本包最关键的负向测试**：
   维度相同（都是 1024）但 `model_version` 不同 → `23514`。
   这是 ADR-15 判据「**混用被阻止**」的唯一落点；
   实测确认**仅比维度拦不住**同维度混用。
7. **混用守卫负向 C（dimensions 列撒谎）**：向量是 1024 维但
   `dimensions` 列写 512 → `23514`（冗余列的值也必须受管）。
8. **唯一约束是 `(chunk_id, profile_id)` 而不是 `chunk_id`**：
   同一 chunk 在两个 profile 各写一行 → **必须都成功**（ADR-15 切换期
   「旧 profile 保持可用」的兑现点）；同一 `(chunk_id, profile_id)`
   重复写 → `23505`。
9. **范围过滤真的下推到 SQL**：造 A / B 两个品牌各一条知识 + 向量，
   subject 只给 A 的 `brand_id` → 断言 **A 在结果里、B 不在**。
   这是验收原文的直接落点。
10. **缺字段 fail-closed**：空 subject → BRAND 知识**全部不可见**；
    同时断言 **GLOBAL 知识仍可见**（否则一个"空 subject 返回空"的实现
    也能让前一条全绿）。
11. **关键词兜底路径同口径**：关键词检索也必须按范围过滤
    （两条路径口径不同的话，"兜底"会变成越权的旁路）。

**外加一条前置条件活性验证**（第 12 条）：新库上 `pgvector` 未装时，
迁移必须**给出可操作的错误**而不是 PG 的 `UndefinedObject`。
本机实测：`createdb` 建的库没有 vector 扩展（只有 `postgres` 主库有）。

## 探针数据的处置

两条检索路径的验证需要**真实提交**（`EXPLAIN` 与跨语句的可见性要求
数据已落库）。因此本脚本在**独立的验证库** `rednote_verify_wp21` 上跑，
且：

  · 探针资产**归档**而不是删除（`knowledge_versions` 有不可变触发器
    `rednote_reject_mutation()`，物理删不掉 —— 这是 WP20 的设计）；
  · 本包的两张表**DROP**（它们没有不可变保护，必须真的干净）；
  · 探针用的品牌 / 产品 / 项目**保留**（`knowledge_assets.brand_id` 是
    RESTRICT，资产行还在就删不掉）。

**本脚本用在哪个库**：默认 `rednote_verify_wp21`（收口时新建的干净库，
迁移从 bootstrap 一路跑到 head 建出全部结构）。用它而不是分支测试库的
理由：测试库里有**测试夹具 create_all 建的 HNSW 索引残留**
（每个测试跑一次就多一个），在那种库上取证据会污染结构计数。

## 探针自身踩过的坑（写在这里，避免重复踩）

1. **不要用替身表结构**。第一版探针用"最小结构"临时建
   `knowledge_assets` / `knowledge_versions` / `knowledge_chunks`，
   结果与真 schema 的差异（`structured_payload` 非空、
   `active_version_id` 指向 `version_nodes` 的延迟外键、
   `brand_id` 的 FK）连续报错 4 次。教训：**验证真品，不验证替身**。
2. **`knowledge_assets.active_version_id` 有跨表延迟约束**（OP-88）：
   写入顺序必须是「建资产（留空）→ 建 `version_nodes` → 建版本 →
   UPDATE 资产」。把 `active_version_id` 写进 INSERT 会撞 FK。
3. **一条语句报错会把整个事务打成 aborted**（后续返回 `25P02`）。
   因此"被拦"与"对照通过"两类探针**必须各自独立事务**。
4. **断言类型 ≠ 断言取值**：弱守卫探针最初复用同一个 chunk，
   结果"被拦下"来自唯一约束（`23505`）而不是守卫（`23514`）——
   判据恒真。必须断言 **sqlstate** 并用全新 chunk。
5. **反向验证会暴露探针自己的无区分力判据**：本包反向验证抓到 3 处
   （段落太短导致坏实现也不超限 / 探针资产没有 chunk 导致任何谓词都
   召回不到 / 弱守卫判定撞唯一约束）。**这三处都是判据的问题，不是实现的问题。**
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"

# 期望条数（与内联脚本里的 EXPECTED_CHECKS 保持同值）。
# 两处都要：内联脚本用它写进 RESULT 行，外层用它判定成功与否（ISS-43）。
EXPECTED_CHECKS: int = 36

PYTHON = sys.executable

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（三引号会提前终止外层包裹，见 ISS-45）。

import re
import uuid

from sqlalchemy import text

from sqlalchemy import create_engine

# 期望产生的核对条数（ISS-43：崩溃伪装成"全过"的防线）。
EXPECTED_CHECKS = 36

DB = "__DB_URL__"
engine = create_engine(DB)

# 本包引入的迁移版本。判据用"不早于"而不是"恰好等于"（ISS-44：
# 硬编码"恰好停在某个版本"会让每个新工作包把历史证据集体打红）。
REVISION_INTRODUCED = "7a4c1e93b6d2"

# 本包 2 张表的期望列数（按 information_schema 实测）。
TABLE_COLUMNS = (
    ("embedding_profiles", 13),
    ("chunk_embeddings", 8),
)

# 本包 CHECK 总数（实测 8：profiles 5 + embeddings 3）。
CHECK_TOTAL = 8

# 本包引入的触发器与函数。
GUARD_TRIGGER = "trg_chunk_embeddings_profile_binding"
GUARD_FUNCTION = "rednote_check_embedding_profile_binding"

# 本包两张表 + 挂在本包表上的迁移索引（零残留判据用）。
OWN_TABLES = ("chunk_embeddings", "embedding_profiles")
OWN_INDEXES = (
    "ix_embedding_profiles_state",
    "ix_chunk_embeddings_profile_id",
    "ix_chunk_embeddings_chunk_id",
)

# "查询计划命中 HNSW"这条判据要造多少向量。
# 实测阈值（pgvector 0.8.6 / PG 17.10 / 1024 维 / m=16 / ef_construction=64）：
#    行数    自然优化器
#      3     未命中（**未 ANALYZE 时会偶然命中** —— 成本平局，见下）
#     50     未命中
#    200     未命中
#    500     未命中
#   1000     命中
# 因此取 2000 留一倍余量。**为什么必须造数据**：行数少时 HNSW 与
# B-tree+Sort 成本几乎相等，平局由统计新鲜度决定 → 判据区分的是
# "统计状态"而不是"实现对不对"（判据纪律第 7 条）。
BULK_ROWS = 2000
BULK_CHUNK_NO = 30000

checks = []


def _revision_is_at_least(current, introduced):
    # 当前 head 是否**不早于** introduced —— 沿真实迁移链判断，不是猜。
    #
    # **为什么不能写 `== EXPECTED_REVISION`**（ISS-44）：那会让每个新工作包
    # 把前面所有包的历史证据集体打红，因为校验的其实是"迁移停在某个特定
    # 版本"，而真正该校验的是"**该包引入的表/约束还在**"（由本脚本后面的
    # 核对项保证）。
    #
    # **为什么也不能用 `len(current) >= len(introduced)`**（ISS-115，2026-09-27
    # 收口轮实测）：本项目全部 16 个 revision id **都是 12 位十六进制**，
    # 于是"长度不短于"在 `current != introduced` 时**恒为 True** ——
    # 那是把等式判据换成了**恒绿判据**，比原缺陷更隐蔽：它永不失败，
    # 因此也永不报警（判据纪律第 7/9 条：判据必须能区分两种实现）。
    # 本项目**存量 12 个脚本全是这个写法**，全部靠它"通过"。
    #
    # 正确做法：拿到**真实迁移链**，按位置比较。`walk_revisions(base=..., head=...)`
    # 的返回值是 head→base 降序，因此 index 小 = 新。
    # ⚠️ 该调用会 import 每个迁移模块（`from rednote.modules...`），
    # 故内联段必须在 `PYTHONPATH=<repo>/services/backend/src` 下运行。
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path.cwd().resolve()
    while repo != repo.parent and not (repo / "migrations" / "alembic.ini").exists():
        repo = repo.parent
    cfg = Config(str(repo / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo / "migrations"))
    chain = [
        r.revision
        for r in ScriptDirectory.from_config(cfg).walk_revisions(
            base="base", head="heads"
        )
    ]
    # 认不出来的 revision（拼错/已删除）一律判否 —— fail-closed。
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def emit(ok, label, detail):
    checks.append(bool(ok))
    print("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def sqlstate(exc):
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


def vec(dims, seed):
    return "[" + ",".join(repr(((seed * 37 + i * 11) % 1000) / 1000.0) for i in range(dims)) + "]"


print("== 1. 迁移版本与结构 ==")
with engine.begin() as conn:
    head = conn.execute(text("select version_num from alembic_version")).scalar()
emit(
    _revision_is_at_least(head, REVISION_INTRODUCED),
    "迁移 head 不早于本包引入的版本",
    str(head) + " (本包引入 " + REVISION_INTRODUCED + ")",
)

with engine.begin() as conn:
    for table, expected in TABLE_COLUMNS:
        actual = conn.execute(
            text(
                "select count(*) from information_schema.columns"
                " where table_name = :t and table_schema = 'public'"
            ),
            {"t": table},
        ).scalar()
        emit(actual == expected, table + " 列数", str(actual) + " (期望 " + str(expected) + ")")

with engine.begin() as conn:
    total = conn.execute(
        text(
            "select count(*) from pg_constraint where contype = 'c'"
            " and conrelid in ('embedding_profiles'::regclass, 'chunk_embeddings'::regclass)"
        )
    ).scalar()
emit(total == CHECK_TOTAL, "CHECK 总数", str(total) + " (期望 " + str(CHECK_TOTAL) + ")")

with engine.begin() as conn:
    triggers = [
        row[0]
        for row in conn.execute(
            text(
                "select tgname from pg_trigger where not tgisinternal"
                " and tgrelid in ('embedding_profiles'::regclass, 'chunk_embeddings'::regclass)"
            )
        )
    ]
emit(triggers == [GUARD_TRIGGER], "本包触发器", triggers)

with engine.begin() as conn:
    exists = conn.execute(
        text("select count(*) from pg_proc where proname = :n"), {"n": GUARD_FUNCTION}
    ).scalar()
emit(exists == 1, "守卫函数存在", exists)

with engine.begin() as conn:
    indexes = [
        row[0]
        for row in conn.execute(
            text(
                "select indexname from pg_indexes where schemaname = 'public'"
                " and tablename in ('embedding_profiles', 'chunk_embeddings')"
                " and indexname not like 'pk_%' and indexname not like 'uq_%'"
                " order by 1"
            )
        )
    ]
emit(
    sorted(indexes) == sorted(OWN_INDEXES),
    "迁移建的普通索引（**不含 HNSW** —— 它按 profile 动态建）",
    indexes,
)

with engine.begin() as conn:
    fks = conn.execute(
        text(
            "select count(*) from pg_constraint where contype = 'f'"
            " and conrelid in ('embedding_profiles'::regclass, 'chunk_embeddings'::regclass)"
        )
    ).scalar()
emit(fks == 3, "外键数（profiles→model_profiles、embeddings→chunks、embeddings→profiles）", fks)

print()
print("== 2. 无维度列（多维度共存的前提） ==")
with engine.begin() as conn:
    row = conn.execute(
        text(
            "select a.atttypmod, t.typname from pg_attribute a"
            " join pg_type t on t.oid = a.atttypid"
            " where a.attrelid = 'chunk_embeddings'::regclass and a.attname = 'embedding'"
        )
    ).one()
emit(row[1] == "vector", "embedding 列类型", row[1])
emit(row[0] == -1, "embedding 列无维度（atttypmod）", row[0])

print()
print("== 3. 探针前置数据 ==")
provider_id = uuid.uuid4()
model_profile_id = uuid.uuid4()
user_id = uuid.uuid4()
brand_a = uuid.uuid4()
brand_b = uuid.uuid4()
prod_a = uuid.uuid4()
proj_a = uuid.uuid4()

with engine.begin() as conn:
    conn.execute(
        text(
            "insert into users (id, issuer, subject, display_name, role, status)"
            " values (:i, 'wp21-probe', :s, 'WP21 探针', 'ADMIN', 'ACTIVE')"
        ),
        {"i": str(user_id), "s": "probe-" + str(user_id)[:8]},
    )
    conn.execute(
        text(
            "insert into brands (id, name, status) values (:b, :n, 'ACTIVE')"
        ),
        {"b": str(brand_a), "n": "WP21 探针品牌A " + str(brand_a)[:8]},
    )
    conn.execute(
        text(
            "insert into brands (id, name, status) values (:b, :n, 'ACTIVE')"
        ),
        {"b": str(brand_b), "n": "WP21 探针品牌B " + str(brand_b)[:8]},
    )
    conn.execute(
        text(
            "insert into products (id, brand_id, name, model_code, status)"
            " values (:p, :b, :n, :c, 'ACTIVE')"
        ),
        {"p": str(prod_a), "b": str(brand_a), "n": "WP21 探针产品", "c": "WP21-P1"},
    )
    conn.execute(
        text(
            "insert into projects (id, project_code, name, brand_id, product_id, owner_id,"
            " phase, target_delivery_count, due_date, timezone)"
            " values (:p, :c, :n, :b, :pr, :u, 'PREPARATION', 1,"
            " (now() + interval '30 days')::date, 'Asia/Shanghai')"
        ),
        {"p": str(proj_a), "c": "WP21-PRJ-" + str(proj_a)[:6], "n": "WP21 探针项目",
         "b": str(brand_a), "pr": str(prod_a), "u": str(user_id)},
    )
    conn.execute(
        text(
            "insert into providers (id, name, capability_types, network_mode, endpoint,"
            " status, policy_version)"
            " values (:i, :n, '[\"EMBEDDING\"]'::jsonb, 'LOCAL', 'http://localhost:9',"
            " 'ACTIVE', 'v1')"
        ),
        {"i": str(provider_id), "n": "wp21-probe-provider-" + str(provider_id)[:6]},
    )
    conn.execute(
        text(
            "insert into model_profiles (id, provider_id, model_name, capabilities)"
            " values (:i, :p, :n, '[\"EMBEDDING\"]'::jsonb)"
        ),
        {"i": str(model_profile_id), "p": str(provider_id), "n": "wp21-probe-model"},
    )
emit(True, "探针前置数据已写（user / 2 品牌 / 产品 / 项目 / provider / model_profile）", "")


def seed_knowledge(session, category, scope, body, keywords, brand_id=None,
                   product_id=None, project_id=None):
    asset_id, version_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.execute(
        text(
            "insert into knowledge_assets (id, category, title, brand_id, product_id,"
            " project_id, scope) values (:i, :c, :t, :b, :p, :pj, :s)"
        ),
        {"i": str(asset_id), "c": category, "t": "WP21 探针-" + str(asset_id)[:6],
         "b": None if brand_id is None else str(brand_id),
         "p": None if product_id is None else str(product_id),
         "pj": None if project_id is None else str(project_id), "s": scope},
    )
    session.execute(
        text(
            "insert into version_nodes (id, entity_type, entity_id, revision_no,"
            " schema_version, payload_hash) values (:i, 'KNOWLEDGE', :a, 1, 1, :h)"
        ),
        {"i": str(version_id), "a": str(asset_id), "h": "a" * 64},
    )
    session.execute(
        text(
            "insert into knowledge_versions (id, asset_id, body, structured_payload,"
            " change_reason, schema_version) values (:i, :a, :b,"
            " '{\"fact.statement\": {\"status\": \"FILLED\", \"value\": \"x\"}}'::jsonb,"
            " 'probe', 1)"
        ),
        {"i": str(version_id), "a": str(asset_id), "b": body},
    )
    session.execute(
        text("update knowledge_assets set active_version_id = :v where id = :a"),
        {"v": str(version_id), "a": str(asset_id)},
    )
    session.execute(
        text(
            "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
            " text_content, text_hash, token_count, locator, keywords)"
            " values (:i, :v, 1, :t, :h, :tc, '{}'::jsonb, :kw)"
        ),
        {"i": str(chunk_id), "v": str(version_id), "t": body,
         "h": "b" * 64, "tc": len(body), "kw": keywords},
    )
    return str(asset_id), str(chunk_id)

with engine.begin() as conn:
    asset_a, chunk_a = seed_knowledge(
        conn, "FACT", "BRAND", "除湿机静音卧室适用", ["除湿", "湿机", "静音", "卧室"],
        brand_id=brand_a,
    )
    asset_b, chunk_b = seed_knowledge(
        conn, "SCENE", "BRAND", "B品牌场景描述", ["场景", "品牌"], brand_id=brand_b,
    )
emit(True, "探针知识已写（2 条 BRAND 知识，分属两个品牌）", "")

profile_1024 = str(uuid.uuid4())
profile_512 = str(uuid.uuid4())
with engine.begin() as conn:
    conn.execute(
        text(
            "insert into embedding_profiles (id, name, model_profile_id, model_version,"
            " dimensions, metric, state) values (:i, :n, :mp, 'probe-model@v1', 1024,"
            " 'COSINE', 'ACTIVE')"
        ),
        {"i": profile_1024, "n": "wp21-probe-1024", "mp": str(model_profile_id)},
    )
    conn.execute(
        text(
            "insert into embedding_profiles (id, name, model_profile_id, model_version,"
            " dimensions, metric, state) values (:i, :n, :mp, 'probe-model@v2', 512,"
            " 'COSINE', 'ACTIVE')"
        ),
        {"i": profile_512, "n": "wp21-probe-512", "mp": str(model_profile_id)},
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " values (gen_random_uuid(), :c, :p, 'probe-model@v1',"
            " cast(:v as vector), :h, 1024)"
        ),
        {"c": chunk_a, "p": profile_1024, "v": vec(1024, 1), "h": "c" * 64},
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " values (gen_random_uuid(), :c, :p, 'probe-model@v1',"
            " cast(:v as vector), :h, 1024)"
        ),
        {"c": chunk_b, "p": profile_1024, "v": vec(1024, 2), "h": "d" * 64},
    )
emit(True, "两个 profile（1024 维 / 512 维）+ 两条向量已写", "")

print()
print("== 4. 活性：守卫负向三条 + 对照组 ==")

def try_insert(chunk_id, profile_id, model_version, dims, value):
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
                    " embedding, chunk_text_hash, dimensions)"
                    " values (gen_random_uuid(), :c, :p, :mv, cast(:v as vector), :h, :d)"
                ),
                {"c": chunk_id, "p": profile_id, "mv": model_version,
                 "v": value, "h": "e" * 64, "d": dims},
            )
        return None
    except Exception as exc:
        return sqlstate(exc)


probe_chunk = str(uuid.uuid4())
with engine.begin() as conn:
    conn.execute(
        text(
            "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
            " text_content, text_hash, token_count, locator, keywords)"
            " select :i, knowledge_version_id, 9001, '守卫探针', :h, 4, '{}'::jsonb,"
            " array['探针'] from knowledge_chunks where id = :c"
        ),
        {"i": probe_chunk, "h": "f" * 64, "c": chunk_a},
    )

probe2 = str(uuid.uuid4())
with engine.begin() as conn:
    conn.execute(
        text(
            "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
            " text_content, text_hash, token_count, locator, keywords)"
            " select :i, knowledge_version_id, 9002, '守卫探针2', :h, 4, '{}'::jsonb,"
            " array['探针'] from knowledge_chunks where id = :c"
        ),
        {"i": probe2, "h": "0" * 64, "c": chunk_a},
    )

state = try_insert(probe_chunk, profile_512, "probe-model@v2", 512, vec(512, 4))
emit(state is None, "对照组：维度与 model_version 都对 -> **写入成功**", state or "成功")

state = try_insert(probe_chunk, profile_1024, "probe-model@v1", 512, vec(512, 5))
emit(state == "23514", "负向 A（异维度混用，512 写进 1024 的 profile）-> 被拦", state)

state = try_insert(probe2, profile_1024, "probe-model@v2", 1024, vec(1024, 6))
emit(state == "23514", "负向 B（**同维度、异 model_version**，ADR-15 唯一落点）-> 被拦", state)

state = try_insert(probe2, profile_1024, "probe-model@v1", 512, vec(1024, 7))
emit(state == "23514", "负向 C（dimensions 列撒谎：向量 1024 维、列写 512）-> 被拦", state)

state = try_insert(chunk_a, profile_1024, "probe-model@v1", 1024, vec(1024, 8))
emit(state == "23505", "重复的 (chunk_id, profile_id) -> 唯一约束拦下", state)

print()
print("== 5. 活性：HNSW 部分索引（不同维度共存 + 命中） ==")
with engine.begin() as conn:
    conn.execute(
        text(
            "create index if not exists ix_wp21_probe_1024 on chunk_embeddings using hnsw"
            " ((embedding::vector(1024)) vector_cosine_ops) with (m = 16, ef_construction = 64)"
            " where profile_id = '" + profile_1024 + "'::uuid"
        )
    )
    conn.execute(
        text(
            "create index if not exists ix_wp21_probe_512 on chunk_embeddings using hnsw"
            " ((embedding::vector(512)) vector_cosine_ops) with (m = 16, ef_construction = 64)"
            " where profile_id = '" + profile_512 + "'::uuid"
        )
    )
emit(True, "两个**不同维度**的部分 HNSW 索引已在同一张表上建成", "1024 / 512")

with engine.begin() as conn:
    defs = {
        row[0]: row[1]
        for row in conn.execute(
            text(
                "select indexname, indexdef from pg_indexes where tablename = 'chunk_embeddings'"
                " and indexname in ('ix_wp21_probe_1024', 'ix_wp21_probe_512')"
            )
        )
    }
emit(len(defs) == 2, "两个索引共存", sorted(defs))
emit(
    all(" WHERE " in d for d in defs.values()),
    "两个索引都是**部分索引**（含 WHERE）",
    [d.split(" WHERE ")[1][:40] for d in defs.values()],
)
emit(
    all("hnsw" in d.lower() for d in defs.values()),
    "两个索引都用 hnsw",
    [("hnsw" in d.lower()) for d in defs.values()],
)

with engine.begin() as conn:
    conn.execute(
        text(
            "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
            " text_content, text_hash, token_count, locator, keywords)"
            " select gen_random_uuid(), (select knowledge_version_id"
            " from knowledge_chunks where id = '" + chunk_a + "'),"
            " " + str(BULK_CHUNK_NO) + " + g, 'bulk',"
            " encode(sha256(g::text::bytea), 'hex'), 4, '{}'::jsonb, array['bulk']"
            " from generate_series(1, " + str(BULK_ROWS) + ") as g"
        )
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " select gen_random_uuid(), c.id, '" + profile_1024 + "'::uuid,"
            " 'probe-model@v1',"
            " (select array_agg(random())::vector(1024) from generate_series(1, 1024)),"
            " encode(sha256(c.id::text::bytea), 'hex'), 1024"
            " from knowledge_chunks c"
            " where c.chunk_no > " + str(BULK_CHUNK_NO)
            + " and c.chunk_no <= " + str(BULK_CHUNK_NO + BULK_ROWS)
        )
    )
    conn.execute(text("analyze chunk_embeddings"))

with engine.begin() as conn:
    rows = conn.execute(
        text("select count(*) from chunk_embeddings where profile_id = '"
             + profile_1024 + "'::uuid")
    ).scalar()
emit(rows >= BULK_ROWS, "为命中判据造足量向量（见 BULK_ROWS 的阈值说明）",
     str(rows) + " 行")

# 正判据：**自然优化器**（不设任何 enable_*）。设 enable_seqscan=off 是
# 削弱条件 —— 真实优化器不靠它，靠它压出来的计划证明不了"索引真的会被用"。
with engine.begin() as conn:
    conn.execute(text("set local enable_seqscan = on"))
    conn.execute(text("set local enable_sort = on"))
    plan = "\n".join(
        row[0]
        for row in conn.execute(
            text(
                "explain select e.id from chunk_embeddings e"
                " join knowledge_chunks c on c.id = e.chunk_id"
                " where e.profile_id = '" + profile_1024 + "'::uuid"
                " order by (e.embedding::vector(1024) <=> cast('" + vec(1024, 1) + "' as vector))"
                " limit 5"
            )
        )
    )
emit(
    "ix_wp21_probe_1024" in plan,
    "查询计划命中**本 profile 的部分 HNSW 索引**（钉索引名，不是笼统 Index Scan）",
    [line.strip()[:70] for line in plan.splitlines() if "Index Scan" in line][:1],
)

# 负判据（**这条是让上一条有区分力的那一半**）：把 ORDER BY 里的
# `::vector(1024)` 去掉，其余完全不变。索引建在表达式上，少了这个 cast
# 就用不上 —— 若两条都命中，说明上一条判据恒真、证明不了任何事。
with engine.begin() as conn:
    conn.execute(text("set local enable_seqscan = on"))
    conn.execute(text("set local enable_sort = on"))
    plan_nocast = "\n".join(
        row[0]
        for row in conn.execute(
            text(
                "explain select e.id from chunk_embeddings e"
                " join knowledge_chunks c on c.id = e.chunk_id"
                " where e.profile_id = '" + profile_1024 + "'::uuid"
                " order by (e.embedding <=> cast('" + vec(1024, 1) + "' as vector))"
                " limit 5"
            )
        )
    )
emit(
    "ix_wp21_probe_1024" not in plan_nocast,
    "负判据：ORDER BY 漏写 cast 时**用不上该索引**（证明上一条不是恒真）",
    [line.strip()[:70] for line in plan_nocast.splitlines()
     if "Scan" in line or "Sort" in line][:1],
)

print()
print("== 6. 活性：范围谓词下推（验收原文） ==")
with engine.begin() as conn:
    rows = list(
        conn.execute(
            text(
                "select a.id from chunk_embeddings e"
                " join knowledge_chunks c on c.id = e.chunk_id"
                " join knowledge_versions v on v.id = c.knowledge_version_id"
                " join knowledge_assets a on a.id = v.asset_id"
                " where e.profile_id = '" + profile_1024 + "'::uuid"
                " and a.active_version_id = v.id and a.archived_at is null"
                " and (a.scope = 'GLOBAL' or (a.scope = 'BRAND' and a.brand_id = '"
                + str(brand_a) + "'::uuid))"
            )
        )
    )
ids = {str(row[0]) for row in rows}
emit(asset_a in ids, "范围过滤：A 品牌的知识被召回", asset_a[:8])
emit(asset_b not in ids, "范围过滤：**B 品牌的知识不在结果里**（验收原文）", asset_b[:8])

with engine.begin() as conn:
    empty_rows = list(
        conn.execute(
            text(
                "select a.id from chunk_embeddings e"
                " join knowledge_chunks c on c.id = e.chunk_id"
                " join knowledge_versions v on v.id = c.knowledge_version_id"
                " join knowledge_assets a on a.id = v.asset_id"
                " where e.profile_id = '" + profile_1024 + "'::uuid"
                " and a.active_version_id = v.id and a.archived_at is null"
                " and (a.scope = 'GLOBAL')"
            )
        )
    )
empty_ids = {str(row[0]) for row in empty_rows}
emit(
    asset_a not in empty_ids and asset_b not in empty_ids,
    "fail-closed：空 subject 时两条 BRAND 知识都不可见",
    len(empty_ids),
)

global_asset, global_chunk = None, None
with engine.begin() as conn:
    global_asset, global_chunk = seed_knowledge(
        conn, "RESEARCH", "GLOBAL", "全局研究结论", ["全局", "研究"]
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " values (gen_random_uuid(), :c, :p, 'probe-model@v1', cast(:v as vector), :h, 1024)"
        ),
        {"c": global_chunk, "p": profile_1024, "v": vec(1024, 9), "h": "1" * 64},
    )
with engine.begin() as conn:
    rows = list(
        conn.execute(
            text(
                "select a.id from chunk_embeddings e"
                " join knowledge_chunks c on c.id = e.chunk_id"
                " join knowledge_versions v on v.id = c.knowledge_version_id"
                " join knowledge_assets a on a.id = v.asset_id"
                " where e.profile_id = '" + profile_1024 + "'::uuid"
                " and a.active_version_id = v.id and a.archived_at is null"
                " and (a.scope = 'GLOBAL')"
            )
        )
    )
gids = {str(row[0]) for row in rows}
emit(
    global_asset in gids,
    "对照组：GLOBAL 知识在空 subject 下**仍可见**（防止『空 subject 返回空』假过）",
    global_asset[:8],
)

with engine.begin() as conn:
    kw_rows = list(
        conn.execute(
            text(
                "select a.id from knowledge_chunks c"
                " join knowledge_versions v on v.id = c.knowledge_version_id"
                " join knowledge_assets a on a.id = v.asset_id"
                " where c.keywords && cast(array['除湿','静音'] as text[])"
                " and a.active_version_id = v.id and a.archived_at is null"
                " and (a.scope = 'GLOBAL' or (a.scope = 'BRAND' and a.brand_id = '"
                + str(brand_a) + "'::uuid))"
            )
        )
    )
kw_ids = {str(row[0]) for row in kw_rows}
emit(asset_a in kw_ids, "关键词路径：A 品牌知识被召回", asset_a[:8])
emit(asset_b not in kw_ids, "关键词路径：**同样按范围过滤**（两路径口径一致）", asset_b[:8])

print()
print("== 7. 活性：切换期两个 profile 共存 + 级联删除 ==")
switch_chunk = str(uuid.uuid4())
with engine.begin() as conn:
    conn.execute(
        text(
            "insert into knowledge_chunks (id, knowledge_version_id, chunk_no,"
            " text_content, text_hash, token_count, locator, keywords)"
            " select :i, knowledge_version_id, 9100, '切换期探针', :h, 6, '{}'::jsonb,"
            " array['切换'] from knowledge_chunks where id = :c"
        ),
        {"i": switch_chunk, "h": "2" * 64, "c": chunk_a},
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " values (gen_random_uuid(), :c, :p, 'probe-model@v1', cast(:v as vector), :h, 1024)"
        ),
        {"c": switch_chunk, "p": profile_1024, "v": vec(1024, 11), "h": "3" * 64},
    )
    conn.execute(
        text(
            "insert into chunk_embeddings (id, chunk_id, profile_id, model_version,"
            " embedding, chunk_text_hash, dimensions)"
            " values (gen_random_uuid(), :c, :p, 'probe-model@v2', cast(:v as vector), :h, 512)"
        ),
        {"c": switch_chunk, "p": profile_512, "v": vec(512, 12), "h": "4" * 64},
    )
with engine.begin() as conn:
    distinct = conn.execute(
        text(
            "select count(distinct profile_id) from chunk_embeddings where chunk_id = :c"
        ),
        {"c": switch_chunk},
    ).scalar()
emit(
    distinct == 2,
    "ADR-15 切换期：同一 chunk 在**两个 profile 各一行**（旧 profile 保持可用）",
    distinct,
)

with engine.begin() as conn:
    conn.execute(text("delete from knowledge_chunks where id = :c"), {"c": switch_chunk})
with engine.begin() as conn:
    left = conn.execute(
        text("select count(*) from chunk_embeddings where chunk_id = :c"),
        {"c": switch_chunk},
    ).scalar()
emit(left == 0, "ON DELETE CASCADE：删 chunk 时向量被级联删除", left)

print()
print("== 8. 零残留 ==")
with engine.begin() as conn:
    conn.execute(text("drop index if exists ix_wp21_probe_1024"))
    conn.execute(text("drop index if exists ix_wp21_probe_512"))
    for table in OWN_TABLES:
        conn.execute(text("drop table if exists " + table + " cascade"))
    conn.execute(text("drop function if exists " + GUARD_FUNCTION + "() cascade"))
    conn.execute(
        text("update knowledge_assets set archived_at = now() where id = any(:ids)"),
        {"ids": [asset_a, asset_b, global_asset]},
    )
    conn.execute(
        text("delete from model_profiles where id = :i"), {"i": str(model_profile_id)}
    )
    conn.execute(text("delete from providers where id = :i"), {"i": str(provider_id)})

with engine.begin() as conn:
    tables = conn.execute(
        text(
            "select count(*) from pg_tables where schemaname = 'public'"
            " and tablename = any(:names)"
        ),
        {"names": list(OWN_TABLES)},
    ).scalar()
emit(tables == 0, "零残留：本包两张表已 DROP（探针索引随表清理）", tables)

with engine.begin() as conn:
    fn = conn.execute(
        text("select count(*) from pg_proc where proname = :n"), {"n": GUARD_FUNCTION}
    ).scalar()
emit(fn == 0, "零残留：守卫函数已 DROP", fn)

with engine.begin() as conn:
    alive = conn.execute(
        text(
            "select count(*) from knowledge_assets where id = any(:ids) and archived_at is null"
        ),
        {"ids": [asset_a, asset_b, global_asset]},
    ).scalar()
emit(alive == 0, "零残留：探针资产已归档（不可变表用归档而非删除）", alive)

print()
complete = len(checks) >= EXPECTED_CHECKS
print("RESULT: " + str(sum(1 for c in checks if c)) + " PASS / "
      + str(sum(1 for c in checks if not c)) + " FAIL / " + str(len(checks))
      + " checks (期望 " + str(EXPECTED_CHECKS) + ")")
if not complete:
    print("INCOMPLETE: 只跑了 " + str(len(checks)) + " 条，不足 " + str(EXPECTED_CHECKS))
raise SystemExit(0 if all(checks) and complete else 1)
"""


DB_NAME = "rednote_verify_wp21"
DB_URL = f"postgresql+psycopg://rednote@localhost:5432/{DB_NAME}"
ADMIN_DSN = "host=localhost port=5432 dbname=postgres user=rednote"


def _prepare_verify_db() -> None:
    """把验证库重建到 head。

    **为什么必须在脚本里做**：本包第 8 节会 `DROP` 自己那两张表做"零残留"
    验证（`knowledge_versions` 有不可变触发器物理删不掉，只能归档；
    但本包两张表没有保护，必须真的干净）。于是脚本跑完，库里就没有
    本包的表了 —— 第二次跑必然失败。

    实测：不加这一步，收口时"复跑全部历史证据脚本"的 for 循环里
    WP21 会 `rc=1`，而**失败原因不是判据被打破，而是库被上次跑消费掉了**。
    这类"脚本不可重复运行"的缺陷不会在第一次跑时暴露。
    """
    import psycopg

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(f"drop database if exists {DB_NAME} with (force)")
        cur.execute(f"create database {DB_NAME}")
    with psycopg.connect(
        f"host=localhost port=5432 dbname={DB_NAME} user=rednote", autocommit=True
    ) as conn:
        # 迁移刻意不代建扩展（扩展属部署层职责，见迁移的 fail-closed 检查），
        # 因此验证库要自己建。
        conn.cursor().execute("create extension if not exists vector")

    env = {
        **os.environ,
        "REDNOTE_DATABASE_URL": DB_URL,
        "PYTHONPATH": str(REPO_ROOT / "services" / "backend" / "src"),
    }
    proc = subprocess.run(
        [PYTHON, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "验证库迁移失败：\n" + (proc.stdout + proc.stderr)[-2000:]
        )


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = EVIDENCE_DIR / "wp21-embedding.log"

    lines: list[str] = []
    lines.append("# WP21 切块 + embedding + 范围过滤 + 检索 —— 证据")
    lines.append("#")
    lines.append("# 采集方式：tools/m1/collect_wp21_evidence.py")
    lines.append(
        "# 验证库：rednote_verify_wp21（**每次采集前重建** + alembic upgrade head 建出）"
    )
    lines.append("#")
    lines.append("# ⚠️ 本文件的**元信息不写死覆盖范围/条数** ——")
    lines.append("#    那些数字会随后续开发生长（ISS-78：写死就注定过期且不报错）。")
    lines.append("#    条数由内联脚本的 RESULT 行现场派生。")
    lines.append("")

    print(f"=== 重建验证库 {DB_NAME} 并升级到 head ===")
    _prepare_verify_db()
    print("    就绪")
    print()
    print("=== 结构核对 + 活性验证 ===")
    result = subprocess.run(
        # 内联段里**不能**用 str.format 注入库名：它有 46 处 JSON 字面量的花括号，
        # 会被当成占位符。用不可能撞车的 `__DB_URL__` 做 replace。
        [PYTHON, "-c", INSPECT_SCRIPT.replace("__DB_URL__", DB_URL)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "services" / "backend" / "src")},
    )
    output = result.stdout + result.stderr
    print(output)
    lines.append(output)

    lines.append("")
    lines.append("# 退出码（0 = 全部核对通过）")
    lines.append(f"$ exit_code = {result.returncode}")
    lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n写入 {log_path}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
