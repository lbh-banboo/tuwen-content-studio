#!/usr/bin/env python
"""WP37 证据采集脚本（链接规范化 / 笔记去重）。

产出：`evidence/m4/wp37-link-normalization-dedup.log`。

目标库 `rednote_verify_wp37`：每次运行 DROP + CREATE + `CREATE EXTENSION vector` +
`alembic upgrade head`；库名写死，不读任何环境变量，且只对这一个名字（及
`rednote_verify_wp37_tmp_<pid>` 一次性临时库）做 DROP / CREATE。

## 判据分组

| 组 | 内容 |
|---|---|
| [M] | 迁移 head 不早于 e5f37c0de005；迁移文件机械判据（不 import 活代码、downgrade 只删自有 FK / 索引 / 列、不碰 `rednote_reject_mutation`） |
| [S] | `publication_links` 结构：project_id NOT NULL + FK RESTRICT、项目内唯一索引恰为 (project_id, canonical_url)、除主键外唯一索引只有这一个、原三个普通索引仍在、与模型逐名对账 |
| [F] | `normalize_url` 纯函数：幂等 / 各规则 / 非法输入 / link_kind 只产出 LONG、OTHER / urls.py 无 IO（AST） |
| [P] | 行为探针（独立事务 + 回滚，不提交）：项目内重复 23505、跨项目允许、project_id NULL 23502、link_kind 非法 CHECK、content_state 无"已删除" |
| [R] | 三个端点在 FastAPI app、`contracts/openapi.json`、`contracts/draft/m4-openapi.yaml` 三处路径一致 |
| [O] | `OrgAction.CONFIRM_CROSS_PROJECT_NOTE`：管理者 True、运营 False |
| [C] | 静态守卫：delivery 包无出站网络 import；WP37 新代码无 note_id 抽取；跨项目 409 的 details 不含对方项目信息 |
| [D] | 临时库 upgrade -> downgrade -1 -> upgrade 往返 |
| [V] | 反向验证：把错误实现放回去（只改事务里的库 / 内存里的坏源码，不改真树文件），判据必须报红 |
| [T] | 外层 pytest：test_delivery_wp37.py + test_delivery_wp36.py，指向目标库 |
| [Z] | 探针零残留；临时库已清；反向验证后目标库原样 |

## 如实登记：本脚本没有覆盖的事项

1. **"短链与长链规范化后不得重复"这条验收，本包不承诺，本脚本也不声称覆盖。**
   按约定本包不做短链识别 / 展开、不做小红书专属规则、不做 note_id 抽取；同一条笔记的
   短链与长链会被当成两条不同链接（已知缺口）。[F] 只证明：`link_kind` 永不产出 SHORT、
   规范化无 IO。
2. 并发唯一冲突转 409（`add_publication_link` 里 `IntegrityError` 分支）**没有真并发用例**；
   [P] 只证明唯一索引会在库里挡下第二行（23505），不证明并发下服务层一定走到 409。
3. `Idempotency-Key` 对 links 端点只校验长度、**不回放**；本脚本不探。
4. `PublicationVerify.note` 接收但**不落库**（`publications` 无对应列）；本脚本不探。
5. 跨项目 409 的 details 不含对方项目 id：[C] 用 service 源码 AST 白名单 kwargs 证明，
   行为层（真请求）由 [T] 的 pytest 覆盖，本脚本不再造 HTTP 请求。
6. `contracts/draft/m4-openapi.yaml` 的 links 描述文字仍写着旧的全局唯一索引名
   `uq_publication_links_canonical`，与现行实现（项目内唯一）不符；本脚本 [R] 只对账路径，
   不对账描述文字。这是文档漂移，不在本脚本修改范围。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "services" / "backend"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m4"

#: 期望条数（实测回填）。INLINE 为内联脚本的 CHECK 条数；总数 = INLINE + 3 条 pytest 判据。
EXPECTED_CHECKS: int = 74
INLINE_EXPECTED: int = 71
PYTEST_EXPECTED_PASSED: int = 150
PYTEST_EXPECTED_SKIPPED: int = 0

DB_NAME = "rednote_verify_wp37"
DB_URL = f"postgresql+psycopg://rednote@localhost:5432/{DB_NAME}"
# pg 工具路径只允许 `tools/ops/drill_db.py::DEFAULT_PG_BIN` 一处定义（回退知识不许复制），这里从它取。
_spec = importlib.util.spec_from_file_location("drill_db", REPO_ROOT / "tools" / "ops" / "drill_db.py")
drill_db = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = drill_db
_spec.loader.exec_module(drill_db)
PG_BIN = Path(drill_db.DEFAULT_PG_BIN)
PSQL = str(PG_BIN / "psql")

PYTHON = sys.executable
TIMEZONE = timezone(timedelta(hours=8))

TEST_FILES = ("tests/test_delivery_wp37.py", "tests/test_delivery_wp36.py")

INSPECT_SCRIPT = r"""
# 内联脚本：只允许单行注释，且不能出现三引号（见 ISS-45）。

import ast
import json
import os
import subprocess
import sys
import types
import uuid
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import rednote.modules.content.models  # noqa: F401
import rednote.modules.exports.models  # noqa: F401
import rednote.modules.files.models  # noqa: F401
import rednote.modules.gateway.models  # noqa: F401
import rednote.modules.identity.models  # noqa: F401
import rednote.modules.jobs.models  # noqa: F401
import rednote.modules.media.models  # noqa: F401
import rednote.modules.projects.models  # noqa: F401
import rednote.modules.prompts.models  # noqa: F401
import rednote.modules.versions.models  # noqa: F401
from rednote.modules.delivery import api as d_api
from rednote.modules.delivery import models as d_models
from rednote.modules.delivery import urls as d_urls
from rednote.modules.projects.permissions import OrgAction, allows_org_action
from rednote.platform.base import Base

EXPECTED_CHECKS = __EXPECTED__

DB = os.environ["WP37_DB_URL"]
REPO = Path(os.environ["WP37_REPO_ROOT"])
engine = create_engine(DB)

DELIVERY_DIR = Path(d_urls.__file__).resolve().parent
URLS_FILE = DELIVERY_DIR / "urls.py"
SERVICE_FILE = DELIVERY_DIR / "service.py"
API_FILE = DELIVERY_DIR / "api.py"
REPO_FILE = DELIVERY_DIR / "repository.py"
MIGRATION_FILE = REPO / "migrations" / "versions" / "20261026_0452_e5f37c0de005_wp37_publication_links_project_unique.py"

WP37_REVISION = "e5f37c0de005"
WP37_DOWN_REVISION = "d4f33c0de004"
TABLE = "publication_links"
UQ_NAME = "uq_publication_links_project_canonical"
FK_NAME = "fk_publication_links_project_id_projects"
ORDINARY_INDEXES = ("ix_publication_links_publication", "ix_publication_links_raw", "ix_publication_links_canonical")
UNIQUE_SQLSTATE = "23505"
NOTNULL_SQLSTATE = "23502"
CHECK_SQLSTATE = "23514"
PROBE_ISSUER = "wp37-probe"
PROBE_PROJECT_NAME = "WP37 探针项目"
PROBE_PROJECT_PREFIX = "WP37PROBE"
CANON = "https://example.com/wp37/probe"

THREE_ROUTES = (
    ("POST", "/publications/{publicationId}/links"),
    ("GET", "/publication-links"),
    ("POST", "/publications/{publicationId}/verifications"),
)


def emit(ok, label, detail):
    print("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None)


def constraint_of(exc):
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def rows(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return [r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions(base="base", head="heads")]


def revision_is_at_least(current, introduced):
    # walk_revisions 从 head 往 base 走：下标越小越新。沿真实链序比较，不用等号。
    chain = revision_chain()
    if current not in chain or introduced not in chain:
        return False
    return chain.index(current) <= chain.index(introduced)


def mutate(source, old, new):
    # 反向验证的坏源码必须真的被改到，否则判据红不红都没意义。
    assert old in source, "变异点不在源码里: " + old
    return source.replace(old, new, 1)


# --- 静态源码判据（可对内存里的坏源码复用）-------------------------------------------
def imports_full(source):
    mods = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
            mods.extend(node.module + "." + a.name for a in node.names)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


NET_BANNED = ("socket", "urllib.request", "urllib3", "http.client", "httpx", "requests", "aiohttp",
              "subprocess", "ftplib", "smtplib", "telnetlib", "websockets", "xmlrpc", "ssl")
PURE_BANNED = NET_BANNED + ("os", "asyncio", "re")


def banned_hits(source, banned):
    hits = []
    for mod in imports_full(source):
        for b in banned:
            if mod == b or mod.startswith(b + "."):
                hits.append(mod)
    return sorted(set(hits))


def j_urls_pure(source):
    hits = banned_hits(source, PURE_BANNED)
    return not hits, "urls.py 命中禁用 import: " + str(hits)


def j_package_no_net(sources):
    bad = {}
    for name, src in sources.items():
        hits = banned_hits(src, NET_BANNED)
        if hits:
            bad[name] = hits
    return not bad, str(len(sources)) + " 个文件；命中: " + str(bad)


def downgrade_code(source):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            return "\n".join(ast.unparse(b) for b in body)
    return ""


def op_calls(source, func):
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "op" and sub.func.attr != "f":
                    out.append(sub.func.attr)
    return sorted(out)


def j_mig_no_live_import(source):
    mods = [m for m in imports_full(source) if m.split(".")[0] == "rednote"]
    return not mods, "迁移里的 rednote 导入: " + str(mods)


def j_mig_downgrade(source):
    code = downgrade_code(source)
    calls = op_calls(source, "downgrade")
    ok = (calls == ["drop_column", "drop_constraint", "drop_index"]
          and "rednote_reject_mutation" not in code
          and "drop_table" not in code and "execute" not in code
          and FK_NAME in code and UQ_NAME in code and "project_id" in code)
    return ok, "downgrade 调用 " + str(calls) + "；点名共用函数=" + str("rednote_reject_mutation" in code)


def j_mig_upgrade(source):
    calls = op_calls(source, "upgrade")
    return calls == ["add_column", "create_foreign_key", "create_index"], "upgrade 调用 " + str(calls)


def pure_names(node):
    # 收集标识符与非 docstring 字符串（用于 note_id 抽取守卫）
    docs = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = sub.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docs.add(id(body[0].value))
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.keyword) and sub.arg:
            out.add(sub.arg)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in docs:
            out.add(sub.value)
    return out


def has_note_id(names):
    return sorted(n for n in names if "noteid" in n.lower().replace("_", ""))


def find_funcs(source, name):
    return [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


NEW_CODE = (
    ("urls.py", None),
    ("service.py", "add_publication_link"),
    ("service.py", "verify_publication"),
    ("repository.py", "find_link_in_project"),
    ("repository.py", "exists_link_in_other_project"),
    ("repository.py", "insert_publication_link"),
    ("repository.py", "list_links_by_canonical"),
    ("repository.py", "backfill_url_canonical"),
    ("api.py", "add_publication_link"),
    ("api.py", "list_publication_links"),
    ("api.py", "verify_publication"),
)


def j_no_note_id(sources):
    # sources: 文件名 -> 源码。每个 WP37 新函数都必须找得到（找不到即红，防止改名后判据空转）。
    hits = {}
    missing = []
    for fname, func in NEW_CODE:
        tree = ast.parse(sources[fname])
        if func is None:
            nodes = [tree]
        else:
            nodes = find_funcs(sources[fname], func)
        if not nodes:
            missing.append(fname + ":" + str(func))
            continue
        for node in nodes:
            found = has_note_id(pure_names(node))
            if found:
                hits[fname + ":" + str(func)] = found
    return (not hits and not missing), "覆盖 " + str(len(NEW_CODE)) + " 处新代码；命中 " + str(hits) + "；找不到 " + str(missing)


def j_cross_details(service_source):
    # 跨项目分支的 DeliveryError(REASON_CROSS_PROJECT_NOTE, ...) 的 kwargs 白名单，且取值不含 project。
    allowed = {"crossProject", "canonicalUrl"}
    calls = []
    for fn in find_funcs(service_source, "add_publication_link"):
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "REASON_CROSS_PROJECT_NOTE":
                calls.append(sub)
    if len(calls) != 1:
        return False, "跨项目分支 DeliveryError 调用数=" + str(len(calls)) + "（应为 1）"
    call = calls[0]
    keys = {k.arg for k in call.keywords}
    leaky = [k.arg for k in call.keywords if "project" in ast.dump(k.value).lower()]
    ok = keys <= allowed and "crossProject" in keys and not leaky and len(call.args) == 2
    return ok, "kwargs=" + str(sorted(keys)) + " 取值含 project 的=" + str(leaky)


# --- 模型侧 / 库侧名字 ----------------------------------------------------------------
def model_names():
    table = Base.metadata.tables[TABLE]
    out = {"c": set(), "f": set(), "i": {str(ix.name) for ix in table.indexes},
           "iu": {str(ix.name) for ix in table.indexes if ix.unique}, "cols": set(table.columns.keys())}
    for con in table.constraints:
        kind = type(con).__name__
        if con.name is None:
            continue
        if kind == "CheckConstraint":
            out["c"].add(str(con.name))
        elif kind == "ForeignKeyConstraint":
            out["f"].add(str(con.name))
    return out


def index_rows(conn):
    sql = (
        "SELECT i.relname, x.indisunique, x.indisprimary, (x.indpred IS NOT NULL),"
        " ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum ORDER BY k.ord)"
        " FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
        " WHERE x.indrelid = CAST(:t AS regclass)"
    )
    return [(r[0], r[1], r[2], r[3], list(r[4])) for r in rows(conn, sql, t=TABLE)]


def j_notnull(conn):
    r = rows(conn, "SELECT is_nullable FROM information_schema.columns WHERE table_schema='public'"
                   " AND table_name = :t AND column_name = 'project_id'", t=TABLE)
    return len(r) == 1 and r[0][0] == "NO", "project_id is_nullable=" + str(r)


def j_fk(conn):
    r = rows(conn, "SELECT c.conname, c.confdeltype, cf.relname,"
                   " ARRAY(SELECT a.attname FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)),"
                   " ARRAY(SELECT a.attname FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attnum = ANY(c.confkey))"
                   " FROM pg_constraint c JOIN pg_class cf ON cf.oid = c.confrelid"
                   " WHERE c.contype = 'f' AND c.conrelid = CAST(:t AS regclass)", t=TABLE)
    mine = [x for x in r if list(x[3]) == ["project_id"]]
    ok = len(mine) == 1 and mine[0][1] == "r" and mine[0][2] == "projects" and list(mine[0][4]) == ["id"] and mine[0][0] == FK_NAME
    return ok, "project_id 的 FK=" + str([(x[0], x[1], x[2]) for x in mine])


def j_uq_exact(conn):
    hit = [x for x in index_rows(conn) if x[0] == UQ_NAME]
    ok = len(hit) == 1 and hit[0][1] and not hit[0][2] and not hit[0][3] and hit[0][4] == ["project_id", "canonical_url"]
    return ok, "索引 " + UQ_NAME + "=" + str(hit)


def j_only_unique(conn):
    uniq = sorted(x[0] for x in index_rows(conn) if x[1] and not x[2])
    ucons = rows(conn, "SELECT conname FROM pg_constraint WHERE contype = 'u' AND conrelid = CAST(:t AS regclass)", t=TABLE)
    return uniq == [UQ_NAME] and not ucons, "除主键外的唯一索引=" + str(uniq) + " 唯一约束=" + str(ucons)


def j_no_global_unique(conn):
    bad = [x[0] for x in index_rows(conn) if x[1] and not x[2] and "project_id" not in x[4]]
    return not bad, "不含 project_id 的唯一索引（全局唯一）=" + str(bad)


def j_ordinary(conn):
    byname = {x[0]: x for x in index_rows(conn)}
    ok = all(n in byname and not byname[n][1] for n in ORDINARY_INDEXES)
    return ok, str({n: (n in byname and not byname[n][1]) for n in ORDINARY_INDEXES})


def j_model_cols(conn):
    db = {r[0] for r in rows(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name = :t", t=TABLE)}
    want = model_names()["cols"]
    return db == want, "模型 " + str(len(want)) + " / 库 " + str(len(db)) + " 差集 " + str(sorted(db ^ want))


def j_model_indexes(conn):
    ix = index_rows(conn)
    db = {x[0] for x in ix if not x[2]}
    dbu = {x[0] for x in ix if x[1] and not x[2]}
    m = model_names()
    return db == m["i"] and dbu == m["iu"], "模型 " + str(sorted(m["i"])) + " 唯一 " + str(sorted(m["iu"])) + " / 库 " + str(sorted(db)) + " 唯一 " + str(sorted(dbu))


def j_model_cons(conn):
    m = model_names()
    dbc = {r[0] for r in rows(conn, "SELECT conname FROM pg_constraint WHERE contype='c' AND conrelid = CAST(:t AS regclass)", t=TABLE)}
    dbf = {r[0] for r in rows(conn, "SELECT conname FROM pg_constraint WHERE contype='f' AND conrelid = CAST(:t AS regclass)", t=TABLE)}
    return dbc == m["c"] and dbf == m["f"], "CHECK 模型 " + str(sorted(m["c"])) + " 库 " + str(sorted(dbc)) + "；FK 模型 " + str(sorted(m["f"])) + " 库 " + str(sorted(dbf))


# --- F：规范化纯函数判据（接受任意模块对象，便于喂坏实现）-----------------------------
VALID_CORPUS = (
    "HTTP://Example.COM:80/a/b/#frag",
    "https://a.com:443/",
    "https://a.com/x//",
    "https://a.com/x?b=2&a=1",
    "https://a.com/x?utm_source=z&a=1&fbclid=q",
    "https://a.com/x?UTM_Medium=1",
    "https://a.com?utm_source=x",
    "https://a.com:8443/x",
    "  https://a.com/x  ",
    "https://a.com/Ab/Cd",
    "https://a.com/x?q=a%20b&q=%E4%B8%AD",
    "https://[::1]:8080/x/?z=1&a=",
    "https://a.com/x?=v",
    "http://a.com",
)

RULE_CASES = (
    ("HTTP://Example.COM:80/a/b/#frag", "http://example.com/a/b", "LONG"),
    ("https://a.com:443/", "https://a.com", "OTHER"),
    ("https://a.com/x//", "https://a.com/x", "LONG"),
    ("https://a.com/x?b=2&a=1", "https://a.com/x?a=1&b=2", "LONG"),
    ("https://a.com/x?utm_source=z&a=1&fbclid=q", "https://a.com/x?a=1", "LONG"),
    ("https://a.com/x?UTM_Medium=1", "https://a.com/x", "LONG"),
    ("https://a.com?utm_source=x", "https://a.com", "OTHER"),
    ("https://a.com:8443/x", "https://a.com:8443/x", "LONG"),
    ("  https://a.com/x  ", "https://a.com/x", "LONG"),
    ("https://a.com/Ab", "https://a.com/Ab", "LONG"),
    ("https://a.com/x#a", "https://a.com/x", "LONG"),
    ("https://a.com/x?a=1", "https://a.com/x?a=1", "LONG"),
)

INVALID_CORPUS = (
    "", "   ", "ftp://a.com/x", "javascript:alert(1)", "https://", "https://u:p@a.com/x",
    "https://a.com:99999/x", "https://a.com/a b", "a.com/x", "https://a.com/\x00",
    "http://" + "a" * 3000,
)


def j_idem(mod):
    bad = []
    for raw in VALID_CORPUS:
        once = mod.normalize_url(raw).canonical
        twice = mod.normalize_url(once).canonical
        if once != twice:
            bad.append((raw, once, twice))
    return not bad, "语料 " + str(len(VALID_CORPUS)) + " 条；非幂等 " + str(bad)


def j_rules(mod):
    bad = []
    for raw, want, kind in RULE_CASES:
        got = mod.normalize_url(raw)
        if got.canonical != want or got.link_kind != kind:
            bad.append((raw, got.canonical, got.link_kind, want, kind))
    return not bad, "规则用例 " + str(len(RULE_CASES)) + " 条；不符 " + str(bad)


def j_invalid(mod):
    bad = []
    for raw in INVALID_CORPUS:
        try:
            got = mod.normalize_url(raw)
            bad.append((raw[:30], got.canonical[:30]))
        except mod.UrlInvalid:
            pass
    return not bad, "非法用例 " + str(len(INVALID_CORPUS)) + " 条；未被拒 " + str(bad)


def j_kind(mod):
    kinds = set()
    for raw in VALID_CORPUS + tuple(c[0] for c in RULE_CASES):
        kinds.add(mod.normalize_url(raw).link_kind)
    return kinds == {"LONG", "OTHER"}, "产出的 link_kind 集合=" + str(sorted(kinds))


def load_urls(source):
    name = "wp37_mut_urls"
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    try:
        exec(compile(source, name, "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


# --- P：行为探针 ---------------------------------------------------------------------
def people_tx(conn):
    uid, brand, prod, proj, other = (uuid.uuid4() for _ in range(5))
    conn.execute(text("INSERT INTO users (id, issuer, subject, display_name, role, status)"
                      " VALUES (:u, :iss, :sub, 'WP37 探针', 'ADMIN', 'ACTIVE')"),
                 {"u": str(uid), "iss": PROBE_ISSUER, "sub": "probe-" + str(uid)})
    conn.execute(text("INSERT INTO brands (id, name, status) VALUES (:b, :n, 'ACTIVE')"),
                 {"b": str(brand), "n": "WP37 品牌 " + str(brand)[:8]})
    conn.execute(text("INSERT INTO products (id, brand_id, name, model_code, status) VALUES (:p, :b, :n, :c, 'ACTIVE')"),
                 {"p": str(prod), "b": str(brand), "n": "WP37 产品 " + str(prod)[:8], "c": "WP37-" + str(prod)[:8]})
    for pid in (proj, other):
        conn.execute(text("INSERT INTO projects (id, project_code, name, brand_id, product_id, owner_id,"
                          " target_delivery_count, due_date) VALUES (:p, :c, :n, :b, :pr, :o, 1,"
                          " (now() + interval '30 days')::date)"),
                     {"p": str(pid), "c": PROBE_PROJECT_PREFIX + str(pid)[:8], "n": PROBE_PROJECT_NAME,
                      "b": str(brand), "pr": str(prod), "o": str(uid)})
    return {"user": str(uid), "a": str(proj), "b": str(other)}


def pub_tx(conn, ids, project_id, content_state="UNVERIFIED"):
    row = uuid.uuid4()
    conn.execute(text("INSERT INTO publications (id, project_id, delivery_id, publication_key, state, content_state,"
                      " idempotency_key, request_hash, created_by)"
                      " VALUES (:i, :p, NULL, :k, 'DRAFT', :cs, NULL, :rh, :by)"),
                 {"i": str(row), "p": project_id, "k": "PK-" + str(row)[:8], "cs": content_state,
                  "rh": "0" * 64, "by": ids["user"]})
    return str(row)


def link_params(pub, project_id, canonical=CANON, kind="LONG"):
    return {"i": str(uuid.uuid4()), "pub": pub, "p": project_id, "raw": canonical, "c": canonical, "k": kind}


LINK_SQL = ("INSERT INTO publication_links (id, publication_id, project_id, raw_url, canonical_url, link_kind)"
            " VALUES (:i, :pub, :p, :raw, :c, :k)")


def attempt(conn, sql, params):
    # 保存点里试写：成功返回 (None, None)（行留在外层事务里）；失败返回 (sqlstate, constraint)，保存点自动回滚。
    try:
        with conn.begin_nested():
            conn.execute(text(sql), params)
    except DBAPIError as exc:
        return sqlstate(exc), constraint_of(exc)
    return None, None


def run_probe(fn, mutator=None):
    # 独立连接 + 事务，永远 rollback，不提交。
    with engine.connect() as conn:
        try:
            if mutator is not None:
                mutator(conn)
            ids = people_tx(conn)
            return fn(conn, ids)
        finally:
            conn.rollback()


def p_dup_same_project(conn, ids):
    p1, p2 = pub_tx(conn, ids, ids["a"]), pub_tx(conn, ids, ids["a"])
    first = attempt(conn, LINK_SQL, link_params(p1, ids["a"]))
    second = attempt(conn, LINK_SQL, link_params(p2, ids["a"]))
    return first, second


def p_cross_project(conn, ids):
    pa, pb = pub_tx(conn, ids, ids["a"]), pub_tx(conn, ids, ids["b"])
    first = attempt(conn, LINK_SQL, link_params(pa, ids["a"]))
    second = attempt(conn, LINK_SQL, link_params(pb, ids["b"]))
    return first, second


def p_null_project(conn, ids):
    pa = pub_tx(conn, ids, ids["a"])
    return attempt(conn, LINK_SQL, link_params(pa, None))


def p_bad_kind(conn, ids):
    pa = pub_tx(conn, ids, ids["a"])
    bad = attempt(conn, LINK_SQL, link_params(pa, ids["a"], CANON + "/k1", "BOGUS"))
    good = [attempt(conn, LINK_SQL, link_params(pa, ids["a"], CANON + "/k-" + k, k)) for k in ("LONG", "OTHER", "SHORT")]
    return bad, good


def p_bad_content_state(conn, ids):
    sql = ("INSERT INTO publications (id, project_id, delivery_id, publication_key, state, content_state,"
           " idempotency_key, request_hash, created_by) VALUES (:i, :p, NULL, :k, 'DRAFT', :cs, NULL, :rh, :by)")

    def ins(cs):
        key = "PK-" + str(uuid.uuid4())[:8]
        return attempt(conn, sql, {"i": str(uuid.uuid4()), "p": ids["a"], "k": key, "cs": cs, "rh": "0" * 64, "by": ids["user"]})

    out = {cs: ins(cs) for cs in ("DELETED", "已删除", "deleted", "VERIFIED", "UNVERIFIED")}
    pid = pub_tx(conn, ids, ids["a"])
    out["UPDATE->DELETED"] = attempt(conn, "UPDATE publications SET content_state = 'DELETED' WHERE id = :i", {"i": pid})
    return out


def dup_ok(res):
    first, second = res
    return first == (None, None) and second[0] == UNIQUE_SQLSTATE and second[1] == UQ_NAME


checks_probe = {}

# =============================================================================
# M：迁移
# =============================================================================
with engine.connect() as conn:
    has_t = conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
    current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none() if has_t else None
    conn.rollback()
emit(has_t and revision_is_at_least(current, WP37_REVISION), "[M] 迁移 head 不早于 WP37 引入点 e5f37c0de005（沿真实链序比较）",
     "current=" + str(current) + " 引入点=" + WP37_REVISION)
mig_source = MIGRATION_FILE.read_text(encoding="utf-8")
emit(("revision: str = '" + WP37_REVISION + "'") in mig_source
     and ("down_revision: str | Sequence[str] | None = '" + WP37_DOWN_REVISION + "'") in mig_source,
     "[M] revision / down_revision 指向 WP33 迁移 d4f33c0de004", WP37_REVISION + " <- " + WP37_DOWN_REVISION)
ok, detail = j_mig_no_live_import(mig_source)
emit(ok, "[M] 迁移不 import 任何活代码（取值域 / 名字都是迁移内字面量）", detail)
ok, detail = j_mig_upgrade(mig_source)
emit(ok, "[M] upgrade 只做 加列 / 建 FK / 建唯一索引", detail)
ok, detail = j_mig_downgrade(mig_source)
emit(ok, "[M] downgrade 只删自有 FK / 索引 / 列，不碰 rednote_reject_mutation，无 execute / drop_table", detail)

# =============================================================================
# S：结构
# =============================================================================
with engine.connect() as conn:
    n = conn.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name = :t"), {"t": TABLE}).scalar_one()
    emit(n == 1, "[S] publication_links 存在", str(n) + "/1")
    for label, judge in (
        ("project_id NOT NULL", j_notnull),
        ("project_id FK 到 projects(id) 且 ON DELETE RESTRICT，名为 " + FK_NAME, j_fk),
        ("唯一索引 " + UQ_NAME + " 恰为 (project_id, canonical_url)，非部分索引", j_uq_exact),
        ("除主键外唯一索引只有这一个，且无唯一约束", j_only_unique),
        ("不存在全局 canonical_url 唯一（任何不含 project_id 的唯一索引）", j_no_global_unique),
        ("原三个普通索引仍在且非唯一", j_ordinary),
        ("列集与模型一致", j_model_cols),
        ("索引名集合 / 唯一性与模型逐名一致", j_model_indexes),
        ("CHECK / FK 名集合与模型逐名一致", j_model_cons),
    ):
        ok, detail = judge(conn)
        emit(ok, "[S] " + label, detail)
    conn.rollback()

# =============================================================================
# F：规范化纯函数
# =============================================================================
for label, judge in (
    ("幂等 normalize(normalize(x)) == normalize(x)", j_idem),
    ("各规则（scheme/host 小写、默认端口、fragment、尾斜杠、参数排序、追踪参数、保留路径大小写）", j_rules),
    ("非法输入一律抛 UrlInvalid", j_invalid),
    ("link_kind 只产出 LONG / OTHER，永不产出 SHORT（两者都可达）", j_kind),
):
    ok, detail = judge(d_urls)
    emit(ok, "[F] " + label, detail)
urls_source = URLS_FILE.read_text(encoding="utf-8")
ok, detail = j_urls_pure(urls_source)
emit(ok, "[F] urls.py 无 IO（AST：不 import socket / urllib.request / http.client / requests / subprocess / os / asyncio / re 等）", detail)

# =============================================================================
# P：行为探针（全部回滚）
# =============================================================================
res = run_probe(p_dup_same_project)
emit(dup_ok(res), "[P] 同项目同 canonical 第二行被唯一索引挡下（23505，约束名 " + UQ_NAME + "；首行放行）", str(res))
first, second = run_probe(p_cross_project)
emit(first == (None, None) and second == (None, None), "[P] 跨项目同 canonical 两行都允许（跨项目命中交服务层判定）", str((first, second)))
res = run_probe(p_null_project)
emit(res[0] == NOTNULL_SQLSTATE, "[P] project_id 为 NULL 被拒（23502）", str(res))
bad, good = run_probe(p_bad_kind)
emit(bad[0] == CHECK_SQLSTATE and "link_kind_valid" in str(bad[1]), "[P] link_kind 非法值被 CHECK 拒（23514）", str(bad))
emit(all(g == (None, None) for g in good), "[P] link_kind 的 LONG / OTHER / SHORT 在库里都放行（对照：取值域含 SHORT，只是服务层永不产出）", str(good))
cs = run_probe(p_bad_content_state)
emit(all(cs[k][0] == CHECK_SQLSTATE and "content_state_valid" in str(cs[k][1]) for k in ("DELETED", "已删除", "deleted", "UPDATE->DELETED")),
     "[P] content_state 写 DELETED / 已删除 / deleted（INSERT 与 UPDATE）都被 CHECK 拒", str(cs))
emit(cs["VERIFIED"] == (None, None) and cs["UNVERIFIED"] == (None, None), "[P] content_state 的 VERIFIED / UNVERIFIED 放行（对照）", str((cs["VERIFIED"], cs["UNVERIFIED"])))
with engine.connect() as conn:
    defs = [r[0] for r in rows(conn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='c'"
                                     " AND conrelid = CAST('publications' AS regclass) AND conname LIKE '%content_state_valid'")]
    conn.rollback()
emit(len(defs) == 1 and "DELETED" not in defs[0].upper() and "已删除" not in defs[0] and "'VERIFIED'" in defs[0] and "'UNVERIFIED'" in defs[0],
     "[P] content_state CHECK 定义里取值域恰为 VERIFIED / UNVERIFIED，没有已删除", str(defs))
from rednote.modules.delivery import types as d_types
emit(set(d_types.CONTENT_STATES) == {"VERIFIED", "UNVERIFIED"}, "[P] types.CONTENT_STATES 与库内取值域一致", str(d_types.CONTENT_STATES))

# =============================================================================
# R：路由对账
# =============================================================================
app_ops = sorted((m, r.path) for r in d_api.router.routes for m in sorted(r.methods or ()))
emit(all(op in app_ops for op in THREE_ROUTES), "[R] delivery router 含三个 WP37 端点", str([op for op in THREE_ROUTES if op in app_ops]))
from rednote.main import create_app
app = create_app()
full_ops = {(m, r.path) for r in app.routes for m in sorted(getattr(r, "methods", None) or ())}
prefixed = {(m, p) for (m, p) in full_ops if p.startswith("/api/v1/")}
stripped = {(m, p[len("/api/v1"):]) for (m, p) in prefixed}
emit(all(op in stripped for op in THREE_ROUTES), "[R] FastAPI app（create_app）路由里存在三个端点（/api/v1 前缀下）", str([op for op in THREE_ROUTES if op in stripped]))
contract = json.loads((REPO / "contracts" / "openapi.json").read_text(encoding="utf-8"))
cops = {(m.upper(), p[len("/api/v1"):]) for p, item in contract["paths"].items() if p.startswith("/api/v1")
        for m in item if m in ("get", "post", "put", "patch", "delete")}
emit(all(op in cops for op in THREE_ROUTES), "[R] contracts/openapi.json 含三个端点", str([op for op in THREE_ROUTES if op in cops]))
draft = yaml.safe_load((REPO / "contracts" / "draft" / "m4-openapi.yaml").read_text(encoding="utf-8"))
dops = {(m.upper(), p) for p, item in draft["paths"].items() for m in item if m in ("get", "post", "put", "patch", "delete")}
emit(all(op in dops for op in THREE_ROUTES), "[R] contracts/draft/m4-openapi.yaml 含三个端点（路径与方法一致）", str([op for op in THREE_ROUTES if op in dops]))
emit(all(op in app_ops and op in stripped and op in cops and op in dops for op in THREE_ROUTES),
     "[R] 三处（app / openapi.json / 草案）对三个端点的路径 + 方法逐条一致", str(THREE_ROUTES))

# =============================================================================
# O：权限
# =============================================================================
emit(hasattr(OrgAction, "CONFIRM_CROSS_PROJECT_NOTE"), "[O] OrgAction.CONFIRM_CROSS_PROJECT_NOTE 存在", "ok")
emit(allows_org_action(is_admin=True, action=OrgAction.CONFIRM_CROSS_PROJECT_NOTE) is True, "[O] 管理者可确认跨项目同笔记", "True")
emit(allows_org_action(is_admin=False, action=OrgAction.CONFIRM_CROSS_PROJECT_NOTE) is False, "[O] 运营不可确认跨项目同笔记", "False")

# =============================================================================
# C：静态守卫
# =============================================================================
pkg_sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(DELIVERY_DIR.glob("*.py"))}
ok, detail = j_package_no_net(pkg_sources)
emit(ok, "[C] delivery 包所有源文件无出站网络 / 子进程 import（AST）", detail)
new_sources = {"urls.py": urls_source, "service.py": pkg_sources["service.py"], "api.py": pkg_sources["api.py"], "repository.py": pkg_sources["repository.py"]}
ok, detail = j_no_note_id(new_sources)
emit(ok, "[C] WP37 新代码（urls.py 与 11 处链接 / 校验函数）无 note_id 抽取（AST：标识符与非 docstring 字符串）", detail)
ok, detail = j_cross_details(pkg_sources["service.py"])
emit(ok, "[C] 跨项目 409 的 details 只含 crossProject / canonicalUrl，不含对方项目 id（service AST 白名单；行为层见 [T]）", detail)

# =============================================================================
# D：临时库往返
# =============================================================================
base_url = make_url(DB)
TMP_NAME = base_url.database + "_tmp_" + str(os.getpid())
tmp_url = base_url.set(database=TMP_NAME)
admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def alembic(*args):
    env = os.environ.copy()
    env["REDNOTE_DATABASE_URL"] = tmp_url.render_as_string(hide_password=False)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
                          cwd=str(REPO), env=env, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr)[-300:]


def catalog(tmp_engine):
    with tmp_engine.connect() as c:
        cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='publication_links'"))}
        ix = index_rows(c)
        funcs = {r[0] for r in c.execute(text("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'"))}
        fks = {r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE contype='f' AND conrelid = CAST('publication_links' AS regclass)"))}
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        c.rollback()
    return cols, {x[0] for x in ix}, funcs, fks, ver


tmp_engine = None
try:
    assert TMP_NAME.startswith("rednote_verify_wp37_tmp_")
    with admin.connect() as c:
        c.execute(text('DROP DATABASE IF EXISTS "' + TMP_NAME + '"'))
        c.execute(text('CREATE DATABASE "' + TMP_NAME + '"'))
    tmp_engine = create_engine(tmp_url)
    with tmp_engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    head = revision_chain()[0]
    code, tail = alembic("upgrade", "head")
    cols_a, ix_a, funcs_a, fks_a, ver_a = catalog(tmp_engine)
    emit(code == 0 and ver_a == head and "project_id" in cols_a and UQ_NAME in ix_a and FK_NAME in fks_a,
         "[D] 临时库：空库 upgrade head 成功，project_id 列 + 唯一索引 + FK 都在", "exit=" + str(code) + " version=" + str(ver_a))
    code, tail = alembic("downgrade", "-1")
    cols_b, ix_b, funcs_b, fks_b, ver_b = catalog(tmp_engine)
    emit(code == 0 and ver_b == WP37_DOWN_REVISION and "project_id" not in cols_b and UQ_NAME not in ix_b and FK_NAME not in fks_b,
         "[D] 临时库：downgrade -1 成功，回到 WP33 迁移，project_id 列 / 唯一索引 / FK 消失", "exit=" + str(code) + " version=" + str(ver_b))
    emit(cols_a - cols_b == {"project_id"} and ix_a - ix_b == {UQ_NAME} and set(ORDINARY_INDEXES) <= ix_b
         and "rednote_reject_mutation" in funcs_b and funcs_a == funcs_b,
         "[D] downgrade 只删自有对象：仅少 project_id 与唯一索引，原三个普通索引与全部函数（含 rednote_reject_mutation）未动",
         "列差 " + str(sorted(cols_a - cols_b)) + " 索引差 " + str(sorted(ix_a - ix_b)))
    code, tail = alembic("upgrade", "head")
    cols_c, ix_c, funcs_c, fks_c, ver_c = catalog(tmp_engine)
    emit(code == 0 and (cols_c, ix_c, funcs_c, fks_c, ver_c) == (cols_a, ix_a, funcs_a, fks_a, ver_a),
         "[D] 再 upgrade head：列 / 索引 / 函数 / FK / 版本与第一次完全一致（往返幂等，project_id 与唯一索引恢复）", "exit=" + str(code) + " version=" + str(ver_c))
finally:
    if tmp_engine is not None:
        tmp_engine.dispose()
    try:
        with admin.connect() as c:
            c.execute(text('DROP DATABASE IF EXISTS "' + TMP_NAME + '" WITH (FORCE)'))
    except Exception as exc:
        print("# 临时库清理失败 " + TMP_NAME + ": " + str(exc))

# =============================================================================
# V：反向验证（只改事务里的库 / 内存里的坏源码；真树文件不动）
# =============================================================================
def rev(label, fn):
    try:
        ok = bool(fn())
        detail = "判据按预期报红" if ok else "判据没有报红（反向验证失败）"
    except Exception as exc:
        ok, detail = False, "反向验证自身异常: " + repr(exc)[:160]
    emit(ok, label, detail)


def red_in_db(mutation_sqls, judge):
    # 在事务里执行变异 DDL，判据必须返回 False；最后回滚。
    with engine.connect() as conn:
        try:
            for sql in mutation_sqls:
                conn.exec_driver_sql(sql)
            return not judge(conn)[0]
        finally:
            conn.rollback()


GLOBAL_UQ = ["DROP INDEX " + UQ_NAME, "CREATE UNIQUE INDEX " + UQ_NAME + " ON publication_links (canonical_url)"]
rev("[V1] 反向：唯一索引改成仅 canonical_url 的全局唯一 → [S] 恰为 (project_id, canonical_url) 判据报红",
    lambda: red_in_db(GLOBAL_UQ, j_uq_exact))
rev("[V2] 反向：同上 → [S] 「不存在全局唯一」判据报红", lambda: red_in_db(GLOBAL_UQ, j_no_global_unique))
rev("[V3] 反向：保留项目内唯一索引、另加一个全局 canonical 唯一索引 → [S] 「只有这一个唯一索引」判据报红",
    lambda: red_in_db(["CREATE UNIQUE INDEX uq_wp37_mut_global ON publication_links (canonical_url)"], j_only_unique))
rev("[V4] 反向：去掉 project_id 的 NOT NULL → [S] NOT NULL 判据报红",
    lambda: red_in_db(["ALTER TABLE publication_links ALTER COLUMN project_id DROP NOT NULL"], j_notnull))
rev("[V5] 反向：去掉 project_id 的 NOT NULL → [P] 「NULL 被拒」探针报红（NULL 行真的写进去了）",
    lambda: run_probe(p_null_project, lambda c: c.exec_driver_sql("ALTER TABLE publication_links ALTER COLUMN project_id DROP NOT NULL"))[0] != NOTNULL_SQLSTATE)
rev("[V6] 反向：摘掉项目内唯一索引 → [P] 「同项目重复被挡」探针报红",
    lambda: not dup_ok(run_probe(p_dup_same_project, lambda c: c.exec_driver_sql("DROP INDEX " + UQ_NAME))))
rev("[V7] 反向：换成全局唯一 → [P] 「跨项目同 canonical 允许」探针报红",
    lambda: run_probe(p_cross_project, lambda c: [c.exec_driver_sql(s) for s in GLOBAL_UQ])[1] == (UNIQUE_SQLSTATE, UQ_NAME))
rev("[V8] 反向：FK 改成 CASCADE → [S] FK RESTRICT 判据报红",
    lambda: red_in_db(["ALTER TABLE publication_links DROP CONSTRAINT " + FK_NAME,
                       "ALTER TABLE publication_links ADD CONSTRAINT " + FK_NAME + " FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE"], j_fk))
def content_state_def(conn):
    d = [r[0] for r in rows(conn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='c'"
                                  " AND conrelid = CAST('publications' AS regclass) AND conname LIKE '%content_state_valid'")]
    return len(d) == 1 and "DELETED" not in d[0].upper()


def cs_name():
    with engine.connect() as c:
        name = rows(c, "SELECT conname FROM pg_constraint WHERE contype='c' AND conrelid = CAST('publications' AS regclass)"
                       " AND conname LIKE '%content_state_valid'")[0][0]
        c.rollback()
    return name


CS_NAME = cs_name()
rev("[V9] 反向：content_state CHECK 加入 DELETED → [P] 取值域判据报红",
    lambda: red_in_db(["ALTER TABLE publications DROP CONSTRAINT " + CS_NAME,
                       "ALTER TABLE publications ADD CONSTRAINT " + CS_NAME + " CHECK (content_state IN ('VERIFIED','UNVERIFIED','DELETED'))"],
                      lambda c: (content_state_def(c), "")))
rev("[V9b] 反向：同上 → [P] 「写 DELETED 被拒」探针报红（DELETED 真的写进去了）",
    lambda: run_probe(p_bad_content_state, lambda c: [c.exec_driver_sql(s) for s in (
        "ALTER TABLE publications DROP CONSTRAINT " + CS_NAME,
        "ALTER TABLE publications ADD CONSTRAINT " + CS_NAME + " CHECK (content_state IN ('VERIFIED','UNVERIFIED','DELETED'))")])["DELETED"][0] is None)


SHORT_SRC = mutate(urls_source, "LINK_KIND_LONG if (path or query) else LINK_KIND_OTHER", "'SHORT'")
rev("[V10] 反向：normalize 产出 SHORT → [F] link_kind 判据报红", lambda: not j_kind(load_urls(SHORT_SRC))[0])
NOTRACK_SRC = mutate(urls_source, "if not _is_tracking(k)", "if True")
rev("[V11] 反向：不去追踪参数 → [F] 规则判据报红", lambda: not j_rules(load_urls(NOTRACK_SRC))[0])
NONIDEM_SRC = mutate(urls_source, 'path = parts.path.rstrip("/")', 'path = parts.path[:-1] if parts.path.endswith("/") else parts.path')
rev("[V12] 反向：只去一个尾斜杠（非幂等实现）→ [F] 幂等判据报红", lambda: not j_idem(load_urls(NONIDEM_SRC))[0])
NOREJECT_SRC = mutate(urls_source, 'if scheme not in _DEFAULT_PORTS:\n        raise UrlInvalid("只支持 http / https 链接。")', "pass")
rev("[V13] 反向：不拒绝非 http(s) scheme → [F] 非法输入判据报红", lambda: not j_invalid(load_urls(NOREJECT_SRC))[0])
rev("[V14] 反向：urls.py 副本里加 import socket → [C]/[F] 无 IO 判据报红",
    lambda: not j_urls_pure("import socket\n" + urls_source)[0])
rev("[V15] 反向：delivery 包某文件副本里加 import requests → [C] 无出站网络判据报红",
    lambda: not j_package_no_net({**pkg_sources, "service.py": "import requests\n" + pkg_sources["service.py"]})[0])
rev("[V16] 反向：urls.py 副本里加 note_id 抽取 → [C] 无 note_id 抽取判据报红",
    lambda: not j_no_note_id({**new_sources, "urls.py": urls_source + "\n\ndef extract(u):\n    note_id = u.rsplit('/', 1)[-1]\n    return note_id\n"})[0])
LEAK_SRC = mutate(pkg_sources["service.py"], "crossProject=True,", "crossProject=True,\n            otherProjectId=str(project_id),")
rev("[V17] 反向：跨项目 409 的 details 加入项目 id → [C] details 白名单判据报红", lambda: not j_cross_details(LEAK_SRC)[0])
DG_SRC = mutate(mig_source, "op.drop_column('publication_links', 'project_id')",
                "op.drop_column('publication_links', 'project_id')\n    op.execute('DROP FUNCTION rednote_reject_mutation()')")
rev("[V18] 反向：downgrade 里多删 rednote_reject_mutation → [M] downgrade 判据报红", lambda: not j_mig_downgrade(DG_SRC)[0])
IMP_SRC = mutate(mig_source, "import sqlalchemy as sa", "import sqlalchemy as sa\nfrom rednote.modules.delivery import types as live_types")
rev("[V19] 反向：迁移 import 活代码 → [M] 不 import 活代码判据报红", lambda: not j_mig_no_live_import(IMP_SRC)[0])

# =============================================================================
# Z：零残留
# =============================================================================
with engine.connect() as conn:
    for t in ("publication_links", "publications"):
        n = conn.execute(text("SELECT count(*) FROM " + t)).scalar_one()
        emit(n == 0, "[Z] 零残留 " + t, str(n) + " 行")
    n = conn.execute(text("SELECT count(*) FROM projects WHERE name = :n"), {"n": PROBE_PROJECT_NAME}).scalar_one()
    emit(n == 0, "[Z] 零残留 projects（探针）", str(n) + " 行")
    n = conn.execute(text("SELECT count(*) FROM users WHERE issuer = :i"), {"i": PROBE_ISSUER}).scalar_one()
    emit(n == 0, "[Z] 零残留 users（探针）", str(n) + " 行")
    n = conn.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE :p"), {"p": "rednote_verify_wp37_tmp_%"}).scalar_one()
    emit(n == 0, "[Z] 一次性临时库已 DROP", str(n) + " 个残留")
    ok_all = all(j(conn)[0] for j in (j_notnull, j_fk, j_uq_exact, j_only_unique, j_no_global_unique, j_ordinary, j_model_cols, j_model_indexes, j_model_cons))
    emit(ok_all, "[Z] 反向验证后目标库结构原样（NOT NULL / FK / 唯一索引 / 普通索引 / 与模型对账）", "全部复原")
    ok_cs = content_state_def(conn)
    emit(ok_cs, "[Z] 反向验证后 content_state CHECK 原样（仍无 DELETED）", str(ok_cs))
    conn.rollback()

print("RESULT_EMITTED (期望 " + str(EXPECTED_CHECKS) + " 条)")
"""


def _now() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(BACKEND_DIR / "src"),
            "WP37_DB_URL": DB_URL,
            "WP37_REPO_ROOT": str(REPO_ROOT),
        }
    )
    return env


def _psql(db: str, sql: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PSQL, "-h", "localhost", "-U", "rednote", "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc", sql],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def rebuild_target_db(env: dict[str, str]) -> list[str]:
    """DROP + CREATE 目标库（库名写死，只动 rednote_verify_wp37）-> vector 扩展 -> alembic upgrade head。"""
    assert DB_NAME == "rednote_verify_wp37"
    notes: list[str] = []
    steps = (
        ("postgres", f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'),
        ("postgres", f'CREATE DATABASE "{DB_NAME}"'),
        (DB_NAME, "CREATE EXTENSION IF NOT EXISTS vector"),
    )
    for db, sql in steps:
        done = _psql(db, sql, env)
        notes.append(f"psql[{db}] {sql} -> rc={done.returncode} {(done.stdout + done.stderr).strip()[:120]}")
        if done.returncode != 0:
            return notes
    alembic_env = dict(env)
    alembic_env["REDNOTE_DATABASE_URL"] = DB_URL
    done = subprocess.run(
        [PYTHON, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=alembic_env,
        capture_output=True,
        text=True,
        check=False,
    )
    notes.append(f"alembic upgrade head -> rc={done.returncode} {(done.stdout + done.stderr).strip()[-160:]}")
    return notes


_PYTEST_SUMMARY_RE = re.compile(r"^(?:=+ )?(\d+ .*?) in [\d.]+s")


def parse_pytest(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        match = _PYTEST_SUMMARY_RE.search(line)
        if match and re.search(r"\d+ (passed|failed|error|skipped)", match.group(1)):
            for number, word in re.findall(r"(\d+) (passed|failed|errors?|skipped|warnings?|deselected)", match.group(1)):
                counts[word.rstrip("s") if word in ("errors", "warnings") else word] = int(number)
    return counts


def run_pytest(env: dict[str, str]) -> tuple[list[str], str]:
    test_env = dict(env)
    test_env["REDNOTE_TEST_DATABASE_URL"] = DB_URL
    completed = subprocess.run(
        [PYTHON, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", *TEST_FILES],
        cwd=str(BACKEND_DIR),
        env=test_env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    counts = parse_pytest(output)
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    lines = [
        f"CHECK {'OK  ' if completed.returncode == 0 and failed == 0 else 'FAIL'} "
        f"[T] pytest {' '.join(TEST_FILES)} 在目标库上退出码 0 且无失败 / 错误: "
        f"rc={completed.returncode} {counts}",
        f"CHECK {'OK  ' if passed == PYTEST_EXPECTED_PASSED else 'FAIL'} "
        f"[T] pytest 通过条数恰为 {PYTEST_EXPECTED_PASSED}（实测回填，防只跑了一半）: passed={passed}",
        f"CHECK {'OK  ' if skipped == PYTEST_EXPECTED_SKIPPED else 'FAIL'} "
        f"[T] pytest 跳过条数恰为 {PYTEST_EXPECTED_SKIPPED}（跳过不能悄悄变多）: skipped={skipped}",
    ]
    tail = "\n".join(output.rstrip().splitlines()[-12:])
    return lines, tail


def collect_wp37() -> tuple[Path, bool]:
    env = _env()
    setup_notes = rebuild_target_db(env)
    setup_ok = all(" rc=0" in note for note in setup_notes) and len(setup_notes) == 4

    if setup_ok:
        inline = INSPECT_SCRIPT.replace("__EXPECTED__", str(EXPECTED_CHECKS))
        completed = subprocess.run(
            [PYTHON, "-c", inline],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        returncode = completed.returncode
        pytest_lines, pytest_tail = run_pytest(env)
    else:
        output = "建库 / 迁移失败，未进入核对：\n" + "\n".join(setup_notes)
        returncode = 1
        pytest_lines, pytest_tail = [], ""

    checks = [line for line in output.splitlines() if line.startswith("CHECK")]
    inline_count = len(checks)
    checks.extend(pytest_lines)
    failures = [line for line in checks if line.startswith("CHECK FAIL")]

    lines = [
        "# WP37 链接规范化 / 笔记去重：迁移机械判据 / publication_links 结构 / 规范化纯函数 /"
        " 行为探针 / 路由三处对账 / 权限 / 静态守卫 / 临时库往返 / 反向验证 / pytest / 零残留",
        f"# 生成时间: {_now()}",
        f"# 目标库: {DB_URL}（本脚本每次 DROP + CREATE + alembic upgrade head 重建）",
        "# 未覆盖（如实登记）: 「短链与长链规范化后不得重复」验收本包不承诺（不做短链 / 小红书专属规则 /"
        " note_id 抽取）；并发唯一冲突转 409 无真并发用例；links 的 Idempotency-Key 只校验长度不回放；"
        "PublicationVerify.note 不落库",
        "# 命令: cd services/backend && python -c '<pg_catalog + 试写(回滚) + AST + 路由 + 临时库 alembic>'"
        " ; pytest -p no:cacheprovider " + " ".join(TEST_FILES),
        "",
        "## 建库与迁移",
        *setup_notes,
        "",
        output.rstrip(),
        *pytest_lines,
        "",
        "## pytest 输出尾部",
        pytest_tail,
        "",
        f"EXIT={returncode}",
        f"RESULT={len(checks) - len(failures)} PASS / {len(failures)} FAIL"
        f"（共 {len(checks)} 条，期望 {EXPECTED_CHECKS} 条；内联 {inline_count} 条 + pytest 3 条）",
    ]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / "wp37-link-normalization-dedup.log"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(checks) >= EXPECTED_CHECKS
    exact = len(checks) == EXPECTED_CHECKS
    return target, (setup_ok and returncode == 0 and not failures and complete and exact)


def main() -> int:
    path, ok = collect_wp37()
    print(f"{'OK' if ok else 'FAIL'}: wrote {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
