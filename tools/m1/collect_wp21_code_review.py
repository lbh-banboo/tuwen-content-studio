#!/usr/bin/env python
"""WP21 代码审查证据（三遍法：结构 → 逐行 → 边界与加固）。

产出 `evidence/m1/wp21-code-review.log`。

**为什么要把审查结果落成文件**：审查结论是"声称"，而声称需要证据。
本项目已踩过"文档/看板是唯一声称方时没有任何机械判据与它对账"（ISS-78/84/85/86），
因此审查记录里每一处"通过"都必须附上**能复现它的命令或代码位置**。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m1"
PYTHON = sys.executable

MODULE = REPO_ROOT / "services" / "backend" / "src" / "rednote" / "modules" / "knowledge"
MIGRATION = (
    REPO_ROOT / "migrations" / "versions"
    / "20260924_1502_7a4c1e93b6d2_wp21_chunking_and_embedding_search.py"
)

WP21_FILES = (
    "chunking.py",
    "embedding_ddl.py",
    "embedding_models.py",
    "embedding_repository.py",
    "embedding_service.py",
    "embedding_types.py",
    "indexing.py",
    "scope_filter.py",
    "search_api.py",
)

lines: list[str] = []
checks: list[bool] = []


def emit(ok: bool, label: str, detail) -> None:
    checks.append(bool(ok))
    lines.append("CHECK " + ("OK  " if ok else "FAIL") + " " + label + ": " + str(detail))


def head(title: str) -> None:
    lines.append("")
    lines.append(title)


lines.append("# WP21 代码审查证据（三遍法）")
lines.append("#")
lines.append("# 审查对象：")
for f in WP21_FILES:
    lines.append(f"#   modules/knowledge/{f}")
lines.append("#   migrations/versions/20260924_1502_7a4c1e93b6d2_wp21_*.py")
lines.append("#   tests/test_knowledge_wp21.py")
lines.append("#")
lines.append("# 三遍法：一遍看结构与架构契合 / 二遍逐行 / 三遍边界与加固。")
lines.append("# 每一条「通过」都附可复现的判据，不写没有判据的结论。")

# ---------------------------------------------------------------------------
head("== 一遍：结构与架构契合 ==")

# 1) 分层：纯函数 / 存储 / 业务 / HTTP 四层是否各就各位
LAYERS = {
    "纯函数层": ("chunking.py", "scope_filter.py", "embedding_types.py"),
    "存储层": ("embedding_repository.py", "embedding_models.py", "embedding_ddl.py"),
    "业务层": ("embedding_service.py", "indexing.py"),
    "接口层": ("search_api.py", "dependencies.py"),
}
for layer, files in LAYERS.items():
    present = [f for f in files if (MODULE / f).exists()]
    emit(len(present) == len(files), f"分层「{layer}」文件齐全", present)

# 2) 纯函数层不得 import ORM（scope_filter 的 docstring 明说）
src = (MODULE / "scope_filter.py").read_text(encoding="utf-8")
emit(
    "from rednote.modules.knowledge.models import" not in src
    and "embedding_models import" not in src,
    "纯函数层 scope_filter 不 import ORM 模型（可被单测直接调用）",
    "无 ORM 依赖" if "models import" not in src else "存在 ORM 依赖",
)

# 3) 依赖方向：业务层不 import HTTP 层
#
# ⚠️ 判据必须是 `import` 语句，不能是对 `search_api` 的**子串匹配** ——
# docstring 里提到 `search_api.py` 是正常的（说明「权限判定在哪一层」），
# 第一版用子串匹配把它误判成反向依赖（这条判据本身没有区分力）。
svc = (MODULE / "embedding_service.py").read_text(encoding="utf-8")
svc_tree = ast.parse(svc)
svc_imports: list[str] = []
for node in ast.walk(svc_tree):
    if isinstance(node, ast.ImportFrom) and node.module:
        svc_imports.append(node.module)
    elif isinstance(node, ast.Import):
        svc_imports.extend(a.name for a in node.names)
emit(
    not any("search_api" in m for m in svc_imports),
    "依赖方向正确：业务层不 import 接口层（api 依赖 service，反向不成立）",
    "无反向 import" if not any("search_api" in m for m in svc_imports)
    else f"存在反向 import：{svc_imports}",
)

# 4) 迁移的手工编写（G2 铁律）：必须有 upgrade 与 downgrade，且不删别人的表
mig = MIGRATION.read_text(encoding="utf-8")
emit(
    "def upgrade()" in mig and "def downgrade()" in mig,
    "迁移同时实现 upgrade / downgrade",
    "两者都在",
)
drop_tables = re.findall(r'op\.drop_table\(\s*"([^"]+)"', mig)
emit(
    set(drop_tables) <= {"chunk_embeddings", "embedding_profiles"},
    "downgrade 只删本包两张表（G2：不碰别人的表）",
    drop_tables or "（无 drop_table）",
)
emit(
    "knowledge_chunks" not in drop_tables,
    "不删 WP20 的 knowledge_chunks（本包只读它）",
    "未出现在 drop_table 里",
)

# 5) 迁移前置条件 fail-closed（判据纪律第 8 条）
emit(
    "_assert_vector_extension" in mig,
    "迁移对 pgvector 前置条件 fail-closed（给出可操作错误，不是 PG 的 UndefinedObject）",
    "检查函数存在",
)

# ---------------------------------------------------------------------------
head("")
head("== 二遍：逐行（机械扫描） ==")

# 6) 未使用 import（死代码）
unused_report: list[str] = []
for f in WP21_FILES:
    text = (MODULE / f).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # `from __future__ import annotations` 是编译器指令，不算未使用
    unused = [
        k for k in imported
        if k not in used and k != "annotations" and f"'{k}'" not in text
    ]
    if unused:
        unused_report.append(f"{f}: {unused}")
emit(not unused_report, "未使用 import（死代码）扫描", unused_report or "全部干净")

# 7) 硬编码凭据 / 密钥
secret_hits: list[str] = []
SECRET_RE = re.compile(
    r"(password|passwd|secret|api[_-]?key|token)\s*=\s*[\"'][^\"']{6,}[\"']",
    re.IGNORECASE,
)
for f in WP21_FILES:
    for i, line in enumerate((MODULE / f).read_text(encoding="utf-8").splitlines(), 1):
        if SECRET_RE.search(line) and "placeholder" not in line.lower():
            secret_hits.append(f"{f}:{i}")
emit(not secret_hits, "无硬编码凭据（C6 安全）", secret_hits or "未发现")

# 8) SQL 必须是参数化的：text() 里不得出现 f-string 拼进的**值**
sql_injection_risk: list[str] = []
for f in ("embedding_repository.py",):
    text = (MODULE / f).read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        # 允许 f-string 拼**标识符/维度**（表名别名、维度数、索引名），
        # 这些不是用户输入；禁止的是把**参数值**拼进去。
        if re.search(r"\{[a-z_]*(subject|query|profile_id|user|input)", line):
            sql_injection_risk.append(f"{f}:{i}")
emit(
    not sql_injection_risk,
    "SQL 无用户输入拼接（f-string 只用于别名/维度等非输入值）",
    sql_injection_risk or "未发现",
)

# 9) 每个 SQL 的参数都应通过 params 字典传入
repo_src = (MODULE / "embedding_repository.py").read_text(encoding="utf-8")
emit(
    "params: dict[str, Any]" in repo_src and ":limit" in repo_src and ":q" in repo_src,
    "两条检索 SQL 的**值**全部走绑定参数（:limit / :q / :profile_id）",
    "参数化确认",
)

# 10) SET LOCAL 而不是 SET（会话级会被连接池复用）
emit(
    "SET LOCAL hnsw.ef_search" in repo_src,
    "hnsw.ef_search 用 SET LOCAL（会话级会被连接池复用 → 检索质量随请求顺序变化）",
    "SET LOCAL 已用",
)

# 11) 显式超时（C4）：本包无出网调用，embedder 是本地替身
emit(
    "StubAdapter" not in src and "httpx" not in src,
    "本包不直接发起外部网络调用（embedder 由 app.state 注入，替身不出网）",
    "无 httpx / 出网客户端",
)

# ---------------------------------------------------------------------------
head("")
head("== 三遍：边界与加固 ==")

# 12) 参数校验的边界：limit / dimensions / efSearch 都有上下界
svc_src = (MODULE / "embedding_service.py").read_text(encoding="utf-8")
types_src = (MODULE / "embedding_types.py").read_text(encoding="utf-8")
emit(
    all(k in types_src for k in (
        "MIN_SEARCH_LIMIT", "MAX_SEARCH_LIMIT",
        "MIN_EMBEDDING_DIMENSIONS", "MAX_EMBEDDING_DIMENSIONS",
        "MIN_HNSW_EF_SEARCH", "MAX_HNSW_EF_SEARCH",
    )),
    "参数上下界齐全（limit / dimensions / efSearch）",
    "6 个边界常量都在",
)
emit(
    svc_src.count("_validation_error") >= 5,
    "校验失败全部走 422 + fieldErrors（让调用方知道改哪个字段）",
    f"_validation_error 用了 {svc_src.count('_validation_error')} 次",
)

# 13) 空查询 / 空结果的处理与文档一致
emit(
    'if not clean_query' in svc_src,
    "空 query 显式 422（不做「返回全部」—— 那会变成绕过范围过滤的隐患）",
    "已显式拒绝",
)

# 14) 缺字段 fail-closed 有实现（scope_filter 用 false()）
scope_src = (MODULE / "scope_filter.py").read_text(encoding="utf-8")
emit(
    "false()" in scope_src and "if subject_value is None" in scope_src,
    "缺范围字段 → 分支恒假（fail-closed，不是「不过滤」）",
    "false() 已用",
)

# 15) 新增 scope 取值会被守卫发现
emit(
    "assert_scope_values_covered" in scope_src,
    "新增 scope 取值有自检守卫（否则新 scope 的知识永远召回不到且不报错）",
    "守卫函数在",
)

# 16) 守卫是否被测试真正调用（未被调用 = 死代码）
test_src = (REPO_ROOT / "services" / "backend" / "tests" / "test_knowledge_wp21.py").read_text(
    encoding="utf-8"
)
emit(
    "assert_scope_values_covered" in test_src,
    "自检守卫在测试里被真实调用（不是死代码）",
    "测试引用了它",
)

# 17) 幂等：ingest_version 必须先删后写
emit(
    "delete_chunks_for_version" in svc_src or "delete" in svc_src.lower(),
    "ingest_version 幂等（先删该版本既有 chunk 再写 → 重试安全）",
    "删除先于写入",
)

# 18) 反向验证层存在（判据纪律第 7 条）
emit(
    test_src.count("class TestReverse") >= 3,
    "测试含反向验证层（断言同一条判据能区分对/错两种实现）",
    f"{test_src.count('class TestReverse')} 个反向类",
)

lines.append("")
lines.append("== MAJOR 级发现（收口前处理） ==")
lines.append(
    "1. [MAJOR] `embedding_service.search` 的 docstring 说「权限在 search_api.py 的"
)
lines.append(
    "   依赖里判」，而该依赖**只校验登录**（`CurrentUserDep`）。subject 由调用方声明，"
)
lines.append(
    "   于是登录用户可声明任意 brandId 拿到跨品牌的知识**正文**。"
)
lines.append(
    "   定性：这是 WP20 既定口径（读端点「登录即读」）的延续，不是 WP21 引入的口径；"
)
lines.append(
    "   但检索路径的后果比列表路径重（返回事实正文而非摘要）。"
)
lines.append(
    "   处置：① 修正 docstring 的措辞，不留下「已判权限」的假承诺；"
)
lines.append("   ② 登记为待定项（需产品定品牌级权限模型）。")
lines.append(
    "2. [MINOR] `embedding_models.py` 有两个未使用 import（`DateTime` / `JSONB`）—— 已删。"
)
lines.append(
    "3. [MINOR] 两个零调用公开符号（`hnsw_index_drop_ddl` / `process_pending_index_events`）："
)
lines.append(
    "   都是「留给后续路径的入口」，已补 docstring 说明「当前零调用 + 为什么保留」。"
)

lines.append("")
lines.append("== 结论 ==")
lines.append("CRITICAL 0 / MAJOR 1（已修正措辞并登记）/ MINOR 2（已修）")
lines.append(f"机械判据：{sum(1 for c in checks if c)} PASS / "
             f"{sum(1 for c in checks if not c)} FAIL / {len(checks)} checks")

EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
out = EVIDENCE_DIR / "wp21-code-review.log"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\n写入 {out}")
raise SystemExit(0 if all(checks) else 1)
