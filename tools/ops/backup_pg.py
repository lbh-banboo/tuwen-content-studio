#!/usr/bin/env python
"""PostgreSQL 备份（WP47 ③）：`pg_dump -Fc` + 可机读 manifest。

## 为什么不是一个 shell 一行 `pg_dump`

WP47 的验收要的是「RPO ≤ 24h / RTO ≤ 8h 有**真跑过**的证据」。`pg_dump` 只产出
一个二进制 dump 文件，**它自己不证明任何事**：

* 不证明 dump 完整（没校验值的话，截断的文件和完好的文件在磁盘上长得一样）；
* 不证明恢复到目标库后**内容一致**（那要在恢复侧比对，见
  `tools/quality/check_backup_restore_drill.py`）；
* 不证明**比对了多少**（少比几类照样全绿 —— 铁律 14）。

因此本脚本除 dump 之外还产出一份 **manifest**：结构指纹 + 行数快照 + 摘要 + sha256。
恢复侧拿同一套算法再算一遍，两边逐项比相等 —— 比对范围由**磁盘上的系统表决定**，
不由人写名单（少一个对象都会被指纹捕捉）。

## 指纹覆盖六类对象（缺一都会改变指纹）

表 / 列(类型+可空+默认) / 约束(含 CHECK·FK·唯一) / 索引(含定义) / 触发器 / 函数。
全部取自 `information_schema` 与 `pg_catalog`，**不读模型代码** —— 比的是
「库里实际有什么」，而不是「我们认为库里该有什么」。

## 每类比对两层：**身份集合** + **规范化定义文本**

| 层 | 比什么 | 为什么分开 |
|---|---|---|
| `objects_sha256` | 对象的**身份三元组**（名字 + 归属 + 类型） | 严格、稳定，无需规范化 ⇒ **硬断言** |
| `defs_sha256` | 定义文本经 `normalize_def()` 规范化后 | PG 的定义文本**没有规范形式**，裸比必然假红 ⇒ 软信号 + 反向验证 |

### 为什么定义文本必须规范化（2026-10-07 实测）

`pg_dump` 把 CHECK 约束**反解析**成 SQL 文本，`pg_restore` 再**解析**回来，
PG 的解析器会把数组字面量的强制转换位置改写：

* 源库：`actor_kind::text = ANY ((ARRAY['SERVICE'::varchar, ...])::text[])`
* 目标库：`actor_kind::text = ANY (ARRAY[('SERVICE'::varchar)::text, ...])`

**语义完全等价**（都是 text 对 text 比较），但文本不等 ⇒ 裸比 DDL 文本必然假红。
这不是备份出错，是**反解析/重解析的规范化迁移**。故定义文本须先过
`normalize_def()`：去空白 → 去 `::类型` 强制转换 → 去冗余括号。

⚠️ 规范化**必然损失一部分灵敏度**（`col::text='1'` 与 `col='1'` 会变同形）⇒
本脚本另有一条判据断言规范化**没有过度抹平**（去重数量不下降），
演练脚本另有注入破坏反向验证。两者缺一，规范化就成了"把判据调绿"。

## 用法

    python tools/ops/backup_pg.py --dsn 'postgresql://rednote@localhost:5432/x' --out /tmp/bak

`--dsn` 省略时读环境变量 `REDNOTE_DATABASE_URL`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pg_bin() -> str:
    """`pg_dump` 所在目录。**委托给 `drill_db.pg_bin()`，本模块不自己回退。**

    首版这里是写死的 `/opt/homebrew/opt/postgresql@17/bin`。本机跑得通，
    CI 上必红（客户端来自 apt，这个目录不存在 ⇒ `pg_dump` not found）——
    与 `drill_db` docstring 里那条「`rednote@localhost` 写死」是**同一族**的坑：
    本机口径被当成通用口径。回退三级（`REDNOTE_PG_BIN` → `which` → Homebrew）
    只允许存在一份，否则将来改一处忘另一处就是静默失败。

    延迟导入而非模块级：本模块有两种被加载姿势 —— `tools/quality/*` 先
    `sys.path.insert(tools/ops)` 再 `import backup_pg`，以及
    `python tools/ops/backup_pg.py` 直接跑。模块级 `import drill_db` 在其中
    一种姿势下是脆的，放到函数里就没有这个前提。
    """
    import drill_db

    return drill_db.pg_bin()

#: 指纹覆盖的对象类别。**新增类别必须同步这里**（铁律 14：覆盖范围由代码写死
#: 也算"人写"，故另有一条判据断言类别数 >= 本值，见 drill 脚本）。
FINGERPRINT_CLASSES: tuple[str, ...] = (
    "tables",
    "columns",
    "constraints",
    "indexes",
    "triggers",
    "functions",
)

#: 指纹 SQL。每条 SQL 的**最后一列是"定义文本"**，前面各列是"身份"
#: （`has_def=True` 的类才有定义列）。全部走系统表 ⇒ 覆盖范围由磁盘决定，
#: 不由人写名单（铁律 14）。
#:
#: ⚠️ **不要在 SQL 里依赖 `order by` 保证稳定性**：`proname` 有重载（不唯一），
#: 只按它排会让行序随机 ⇒ 指纹随机。统一在 Python 侧**排序后**再哈希。
_SQL_TABLES = """
select table_name from information_schema.tables
where table_schema = 'public' and table_type = 'BASE TABLE'
order by table_name
"""
_SQL_COLUMNS = """
select table_name, column_name, data_type, is_nullable, coalesce(column_default,'')
from information_schema.columns
where table_schema = 'public' order by table_name, column_name
"""
_SQL_CONSTRAINTS = """
select c.conname, c.contype, c.conrelid::regclass::text,
       coalesce(pg_get_constraintdef(c.oid),'')
from pg_constraint c
join pg_class t on t.oid = c.conrelid
join pg_namespace n on n.oid = t.relnamespace
where n.nspname = 'public' order by 1
"""
_SQL_INDEXES = """
select i.relname, t.relname, coalesce(pg_get_indexdef(i.oid),'')
from pg_class i
join pg_namespace n on n.oid = i.relnamespace
join pg_index x on x.indexrelid = i.oid
join pg_class t on t.oid = x.indrelid
where n.nspname = 'public' and i.relkind = 'i' order by 1
"""
_SQL_TRIGGERS = """
select tgname, tgrelid::regclass::text, coalesce(pg_get_triggerdef(oid),'')
from pg_trigger where not tgisinternal order by 1
"""
#: 函数。⚠️ **聚合函数不能用 `pg_get_functiondef` 渲染**：PG 会抛
#: `WrongObjectType: "avg" is an aggregate function`（2026-10-07 实测踩到）。
#: 但把聚合整类剔掉就是**缩小比对范围**（铁律 14），因此这里对聚合改用
#: 「名字 + 参数类型 + 返回类型」渲染 —— 对象**存在性**照样进指纹。
#: 身份列含 `oidvectortypes(proargtypes)` ⇒ 重载函数彼此可区分。
_SQL_FUNCTIONS = """
select p.proname, oidvectortypes(p.proargtypes),
       case when p.prokind = 'a'
            then '<aggregate>(' || oidvectortypes(p.proargtypes) || ')->'
                 || pg_get_function_result(p.oid)
            else pg_get_functiondef(p.oid) end
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' order by 1, 2
"""

#: (类别名, SQL, 是否有"定义文本"列)
_FINGERPRINT_SQLS: tuple[tuple[str, str, bool], ...] = (
    ("tables", _SQL_TABLES, False),
    ("columns", _SQL_COLUMNS, False),
    ("constraints", _SQL_CONSTRAINTS, True),
    ("indexes", _SQL_INDEXES, True),
    ("triggers", _SQL_TRIGGERS, True),
    ("functions", _SQL_FUNCTIONS, True),
)

# ---- 定义文本规范化 ----------------------------------------------------
#: `::numeric(10,2)` / `::character varying` / `::text[]` 之类的强制转换。
_CAST_RE = re.compile(
    r"::\s*[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*"
    r"(?:\s*\([^()]*\))?(?:\s*\[\s*\])*"
)
#: 只裹了一个原子（字符串字面量 / 标识符 / 数字）的冗余括号。
_ATOM_PAREN_RE = re.compile(
    r"\(\s*('(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?)\s*\)"
)
#: 双层括号 `((X))` → `(X)`。
_DOUBLE_PAREN_RE = re.compile(r"\(\s*\(([^()]*)\)\s*\)")


def normalize_def(text: str) -> str:
    """把 PG 反解析出来的定义文本规范化到**可跨 dump/restore 比较**的形式。

    PG 的定义文本没有规范形式：`pg_dump` 反解析 → `pg_restore` 再解析之后，
    数组字面量上的强制转换会从「整体转换」变成「逐元素转换」（语义等价）。
    裸比文本必然假红，故此处抹掉三类**已实测的**无关差异：

    1. 空白；
    2. `::类型` 强制转换；
    3. 冗余括号（裹单个原子 / 双层）。

    ⚠️ 代价：规范化**会损失灵敏度**（`col::text='1'` 与 `col='1'` 变同形）。
    因此调用方必须另有判据断言「没有过度抹平」+ 注入破坏反向验证，
    否则本函数只是"把判据调绿的旋钮"。
    """
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    s = _CAST_RE.sub("", s)
    for _ in range(5):
        prev = s
        s = _ATOM_PAREN_RE.sub(r"\1", s)
        s = _DOUBLE_PAREN_RE.sub(r"(\1)", s)
        if s == prev:
            break
    return re.sub(r"\s+", "", s)


def _sha256_lines(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _dsn_to_libpq(dsn: str) -> str:
    """把 SQLAlchemy 方言 DSN 转成 libpq 认识的 DSN。

    ISS-174 的同一类坑：`postgresql+psycopg://` 是 SQLAlchemy 的方言名，libpq
    不认，喂给 `psycopg.connect()` / `pg_dump` 会抛错并被 `except` 吞成
    "连不上库"。这里只做前缀替换（不新增 DSN 字面量，避免被
    `check_test_db_isolation.py` 抓）。
    """
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_fingerprint(conn) -> dict[str, object]:
    """算结构指纹：六类对象各自 `{count, objects_sha256, defs_sha256, distinct_*}`。

    * `objects_sha256` —— 身份三元组的**排序后**集合摘要（硬断言用）
    * `defs_sha256`    —— `身份|规范化定义` 的**排序后**集合摘要（软信号用）
    * `distinct_raw_defs` / `distinct_norm_defs` —— 供"规范化没有过度抹平"判据

    ⚠️ **必须排序后哈希**：`proname` 有重载，`order by` 只按名字排会让行序随机，
    指纹随之随机（2026-10-07 实测踩到：142 个函数排序不稳定）。
    """
    import psycopg

    out: dict[str, object] = {}
    for name, sql, has_def in _FINGERPRINT_SQLS:
        rows = conn.execute(sql).fetchall()
        identities: list[str] = []
        defs: list[str] = []
        raw_defs: list[str] = []
        for r in rows:
            cells = ["" if c is None else str(c) for c in r]
            identities.append("|".join(cells[:-1] if has_def else cells))
            if has_def:
                defs.append("|".join([identities[-1], normalize_def(cells[-1])]))
                raw_defs.append(cells[-1])
        entry: dict[str, object] = {
            "count": len(rows),
            "objects_sha256": _sha256_lines(identities),
        }
        if has_def:
            entry["defs_sha256"] = _sha256_lines(defs)
            entry["distinct_raw_defs"] = len(set(raw_defs))
            entry["distinct_norm_defs"] = len({normalize_def(d) for d in raw_defs})
        out[name] = entry
    assert isinstance(conn, psycopg.Cursor)
    return out


def row_counts(conn) -> dict[str, int]:
    """每张表的行数快照。**由系统表派生表名**（铁律 14），不写死名单。"""
    tables = [r[0] for r in conn.execute(_SQL_TABLES).fetchall()]
    counts: dict[str, int] = {}
    for t in tables:
        counts[t] = int(conn.execute(f'select count(*) from public."{t}"').fetchone()[0])
    return counts


def business_digest(conn) -> dict[str, str]:
    """业务侧摘要：证明「恢复回来的**数据**一样」，而不只是「结构一样」。

    WP47 原文写的是比对 `content_digest`，但**全仓 0 处该列**（2026-10-07 复核
    确认）⇒ 改用真正存在的 `version_nodes.payload_hash`。
    """
    rows = conn.execute(
        "select payload_hash from public.version_nodes order by payload_hash"
    ).fetchall()
    canon = "\n".join(r[0] for r in rows)
    return {
        "version_nodes_rows": str(len(rows)),
        "payload_hash_set_sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    }


def dump(dsn: str, out_dir: Path) -> tuple[Path, dict[str, object]]:
    """跑 `pg_dump -Fc`，产出 dump 文件 + manifest.json。返回 (dump 路径, manifest)。"""
    import psycopg

    out_dir.mkdir(parents=True, exist_ok=True)
    libpq = _dsn_to_libpq(dsn)
    dbname = libpq.rsplit("/", 1)[-1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_path = out_dir / f"{dbname}_{stamp}.dump"

    pg_bin = _pg_bin()
    env = dict(os.environ, PATH=f"{pg_bin}:{os.environ.get('PATH','')}")
    t0 = time.monotonic()
    proc = subprocess.run(
        [f"{pg_bin}/pg_dump", "-Fc", "--no-owner", "--no-acl", "-f", str(dump_path), libpq],
        capture_output=True,
        text=True,
        env=env,
    )
    dump_seconds = round(time.monotonic() - t0, 3)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump 失败（exit {proc.returncode}）：{proc.stderr[-2000:]}")

    with psycopg.connect(libpq) as conn:
        with conn.cursor() as cur:
            pg_version = cur.execute("select version()").fetchone()[0]
            manifest: dict[str, object] = {
                "schema": "rednote-backup-manifest/v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "database": dbname,
                "pg_version": pg_version,
                "dump_seconds": dump_seconds,
                "fingerprint": compute_fingerprint(cur),
                "row_counts": row_counts(cur),
                "business_digest": business_digest(cur),
            }

    manifest["dump_file"] = dump_path.name
    manifest["dump_bytes"] = dump_path.stat().st_size
    manifest["dump_sha256"] = sha256_file(dump_path)
    (out_dir / f"{dbname}_{stamp}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return dump_path, manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="PostgreSQL 备份（pg_dump -Fc + manifest）")
    ap.add_argument("--dsn", default=os.environ.get("REDNOTE_DATABASE_URL", ""))
    ap.add_argument("--out", required=True, help="备份输出目录")
    args = ap.parse_args()
    if not args.dsn:
        print("FAIL：缺 --dsn，且环境变量 REDNOTE_DATABASE_URL 为空")
        return 1
    try:
        dump_path, manifest = dump(args.dsn, Path(args.out))
    except Exception as exc:  # noqa: BLE001 - 备份脚本要把失败讲清楚
        print(f"FAIL：{type(exc).__name__}: {exc}")
        return 1
    fp = manifest["fingerprint"]  # type: ignore[assignment]
    print(f"OK   dump   {dump_path}")
    print(f"     bytes  {manifest['dump_bytes']}  sha256 {manifest['dump_sha256']}")
    print(f"     指纹   {fp}")  # type: ignore[arg-type]
    print(f"     行数   {sum(manifest['row_counts'].values())} 行 / "  # type: ignore[attr-defined]
          f"{len(manifest['row_counts'])} 表")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    sys.exit(main())
