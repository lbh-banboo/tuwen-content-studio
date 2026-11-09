"""Alembic 环境配置（WP06 骨架）。

**数据库 URL 只从环境变量读，禁止硬编码（C6）。**

读取顺序（第一个命中的生效）：
    1. REDNOTE_DATABASE_URL   —— 与后端进程同一套配置来源，避免"应用连 A 库、
                                 迁移改 B 库"的错位。
    2. ALEMBIC_DATABASE_URL   —— 逃生通道，供 CI/运维在不改应用配置时覆盖。
未命中任一时直接报错退出，不回退到任何默认值。理由：迁移是最不能"猜"的操作 ——
一个隐式默认值会让 `alembic upgrade head` 静默作用到错误的库上。

**这里不做 target_metadata 自动生成（autogenerate）的模型注册。**
M1 骨架期没有业务表（那是 WP11/WP12 的事），且研发计划 01 §5 要求
"HTTP DTO、领域对象、存储模型分离"。业务模型就位后，在此处 import 它们的
metadata 即可启用 `--autogenerate`；现在留空是刻意的，不是遗漏。

**超时同样显式配置（C4）。** 迁移可能跑很久（大表加索引），因此这里的
statement_timeout 不设 —— 但 connect_timeout 必须设，否则网络黑洞时
`alembic upgrade` 会无上限挂住 CI。若要给长迁移设语句超时，用
`ALEMBIC_STATEMENT_TIMEOUT_MS` 显式注入，而不是依赖默认值。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# ---------------------------------------------------------------------------
# 路径与导入
# ---------------------------------------------------------------------------
# 迁移脚本需要 import 应用侧代码时（例如未来引入存储模型的 metadata），
# 必须能把 services/backend/src 放进 sys.path。用相对本文件的定位而非 cwd。
_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[1]
_BACKEND_SRC = _REPO_ROOT / "services" / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

# Alembic Config 对象，提供对 alembic.ini 的访问。
config = context.config

# 按 alembic.ini 的 [loggers]/[handlers]/[formatters] 段初始化日志。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# metadata 来源（WP08 起启用）。
#
# 为什么必须显式 import 各模块的 models：
# SQLAlchemy 的 metadata 是"被 import 过的模型类"的注册表。若只 import Base，
# metadata 里是空的 —— autogenerate 会认为"所有表都该被删掉"，
# 生成出危险的 DROP TABLE 迁移。这是 autogenerate 最典型的翻车方式。
#
# 因此每新增一个含模型的模块，都要在这里补一行 import。
# 顺序无关（模型间通过字符串外键名互相引用）。
from rednote.modules.audit import models as _audit_models  # noqa: F401
# WP25 供应商计价 / 预算并发预占 / 费用账本：pricing_versions /
# cost_estimates / project_budgets / budget_reservations / usage_ledger
# 五张表。不 import 就会在 autogenerate 里被当成"多余的表"而生成 DROP
# —— 见上方注释。其中 pricing_versions 与 usage_ledger 是**不可变表**
# （install 同名的不可变触发器）。
# WP32 扩产 / 批产：batches（可变，仅命令位）+ batch_items（不可变）+
# batch_item_runs（不可变）三张表，另有 6 个触发器（见 batches/ddl.py）；
# 并给 jobs.batch_id 补外键。不 import 就不会进 metadata。
from rednote.modules.batches import models as _batches_models  # noqa: F401
from rednote.modules.billing import models as _billing_models  # noqa: F401
# WP22 信息审核与基线：information_drafts / production_baselines /
# baseline_items / step_approvals 四张表。不 import 就会在 autogenerate 里
# 被当成"多余的表"而生成 DROP —— 见上方注释。
from rednote.modules.baseline import models as _baseline_models  # noqa: F401
from rednote.modules.brief import models as _brief_models  # noqa: F401
# WP26 文案结构生成与标题候选：content_items / content_versions /
# narrative_blueprints / title_candidates / generation_contexts 五张表。
# 不 import 就会在 autogenerate 里被当成"多余的表"而生成 DROP —— 见上方注释。
# 其中后四张是**不可变表**（install 同名的不可变触发器），且
# `content_versions` / `narrative_blueprints` / `title_candidates` /
# `generation_contexts` 之外还挂了 OP-88 的正反两向延迟触发器（见 content/ddl.py）。
from rednote.modules.content import models as _content_models  # noqa: F401
# WP36 KOC 记录/交付与替换/发布登记：koc_records（可变）+ deliveries（可变，
# 列级冻结触发器）+ publications（可变）+ publication_links（可变）四张表。
# 不 import 就不会进 metadata。
from rednote.modules.delivery import models as _delivery_models  # noqa: F401
from rednote.modules.events import models as _event_models  # noqa: F401
# WP35 固定版本 ZIP 导出：export_items 一张表（不可变；另装一个延迟约束触发器
# 在提交时守组内一致，见 exports/ddl.py）。不 import 就不会进 metadata。
from rednote.modules.exports import models as _export_models  # noqa: F401
# WP29 预览/编辑/反馈定位：content_feedback_items（可变）+
# content_feedback_events / content_reviews（不可变）三张表，另有一个
# 锚点一致性延迟触发器（见 feedback/ddl.py）。不 import 就不会进 metadata。
from rednote.modules.feedback import models as _feedback_models  # noqa: F401
from rednote.modules.files import models as _file_models  # noqa: F401
from rednote.modules.gateway import models as _gateway_models  # noqa: F401
from rednote.modules.identity import models as _identity_models  # noqa: F401
from rednote.modules.jobs import models as _job_models  # noqa: F401
from rednote.modules.knowledge import embedding_models as _embedding_models  # noqa: F401
from rednote.modules.knowledge import models as _knowledge_models  # noqa: F401
# WP27 静态图适配与产品参考：media_items / media_versions / reference_assets /
# media_renditions / media_generation_contexts 五张表。不 import 就会在
# autogenerate 里被当成"多余的表"而生成 DROP —— 见上方注释。
# 其中 `media_versions` / `reference_assets` / `media_renditions` /
# `media_generation_contexts` 是**不可变表**（install 同名的不可变触发器）；
# 另有一条「MEDIA 类型节点必须配对到本模块实体」的触发器挂在 **WP11 的
# `version_nodes`** 上（`trg_version_nodes_media_paired` —— 本模块是第 6 个
# `*_node_paired` 触发器，见 media/ddl.py 的 `all_trigger_ddl()`）。
from rednote.modules.media import models as _media_models  # noqa: F401
from rednote.modules.outbox import models as _outbox_models  # noqa: F401
from rednote.modules.parsing import models as _parsing_models  # noqa: F401
# WP24 场景形式草稿与数量配置：runtime_settings / plan_drafts 两张表。
# 不 import 就会在 autogenerate 里被当成"多余的表"而生成 DROP —— 见上方注释。
from rednote.modules.planning import models as _planning_models  # noqa: F401
# WP31 试产关口 / 淘汰补产：trial_rounds（可变）+ trial_round_slots（可变）+
# trial_gate_decisions（不可变）三张表，另有 6 个触发器（见 planning/trial/ddl.py）。
# 不 import 就不会进 metadata。
from rednote.modules.planning.trial import models as _trial_models  # noqa: F401
from rednote.modules.projects import models as _project_models  # noqa: F401
from rednote.modules.prompts import models as _prompt_models  # noqa: F401
# WP33 重复检查 / 质量报告：quality_reports（可变，仅投影列）+ quality_report_versions /
# quality_report_items / quality_dimension_results / quality_similarity_pairs /
# quality_media_alignments / quality_report_artifacts（不可变）七张表，另有 13 个触发器
#（见 quality/ddl.py）。不 import 就不会进 metadata。
from rednote.modules.quality import models as _quality_models  # noqa: F401
# WP30 AI 自检/硬软规则：self_check_runs（不可变）+ self_check_rule_results
# （不可变）+ self_check_findings（不可变）三张表，另有两个延迟一致性触发器
# （见 review/ddl.py）。不 import 就不会进 metadata。
from rednote.modules.review import models as _review_models  # noqa: F401
from rednote.modules.versions import models as _version_models  # noqa: F401
# WP34 回退影响图 / 导出失效：export_validity_events（不可变、追加写）一张表，
# 另有 2 个触发器（不可变保护 + 追加一致性，见 versions/impact/ddl.py）。不 import 就不会进 metadata。
from rednote.modules.versions.impact import models as _version_impact_models  # noqa: F401
# WP10 的任务对象 work_items。**这一行是补的** —— 此前 17 行 import 里独缺它，
# 属自 WP10 起的盲区：`alembic revision --autogenerate` 会把 work_items
# 当成"metadata 里没有、库里却有"的表，进而生成 `op.drop_table('work_items')`。
# 之所以一直没爆，是因为手动 autogenerate 才触发，而 CI 只跑 `upgrade head`。
# 现由 `tools/quality/check_migration_model_parity.py` 的
# `_env_py_imported_modules()` 对账（磁盘上的模块 ⟷ 本文件的 import），
# 再漏就会报红。
from rednote.modules.work_items import models as _work_item_models  # noqa: F401
from rednote.platform.base import Base

target_metadata = Base.metadata

# 迁移脚本的公共 schema 过滤：不迁移 pg_catalog 等系统 schema。
# 留作 autogenerate 启用后的白名单入口。
_INCLUDE_SCHEMAS: tuple[str, ...] | None = None

# 连接超时（秒）。显式给值，禁止 None —— 见模块 docstring。
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


def _resolve_database_url() -> str:
    """从环境变量解析 DSN。未配置则明确失败。"""
    for name in ("REDNOTE_DATABASE_URL", "ALEMBIC_DATABASE_URL"):
        raw = os.environ.get(name, "").strip()
        if raw:
            if not raw.startswith("postgresql+psycopg://"):
                # 只回显方言要求，绝不回显 DSN 内容（可能含口令）。
                raise RuntimeError(
                    f"{name} must use the psycopg 3 dialect "
                    "(start with 'postgresql+psycopg://')"
                )
            return raw

    raise RuntimeError(
        "database URL is not configured. Set REDNOTE_DATABASE_URL "
        "(or ALEMBIC_DATABASE_URL) before running alembic. "
        "Example: export REDNOTE_DATABASE_URL="
        "'postgresql+psycopg://<user>@<host>:5432/<db>'"
    )


def _connect_args() -> dict[str, object]:
    """连接参数。connect_timeout 必须显式；statement_timeout 可选注入。"""
    connect_timeout = int(
        os.environ.get("ALEMBIC_CONNECT_TIMEOUT_SECONDS", _DEFAULT_CONNECT_TIMEOUT_SECONDS)
    )
    args: dict[str, object] = {
        "connect_timeout": connect_timeout,
        "application_name": "rednote-alembic",
    }
    statement_timeout_ms = os.environ.get("ALEMBIC_STATEMENT_TIMEOUT_MS", "").strip()
    if statement_timeout_ms:
        # 只在显式配置时下发。长迁移（大表加索引）默认不该被语句超时打断，
        # 但 CI 想快速失败时可以注入一个上限。
        args["options"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    return args


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（`alembic upgrade head --sql`）。

    离线模式下 Alembic 需要 DSN 来判断方言（psycopg 的占位符与转义规则与
    其他方言不同），因此仍然要求配置 URL，但不建立任何连接。
    """
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=_INCLUDE_SCHEMAS is not None,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：真连库执行迁移。"""
    connectable = create_engine(
        _resolve_database_url(),
        poolclass=pool.NullPool,
        connect_args=_connect_args(),
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=_INCLUDE_SCHEMAS is not None,
            compare_type=True,
            compare_server_default=True,
            # 事务化 DDL：PostgreSQL 支持事务内 DDL，因此迁移失败可以整体回滚。
            # 这让"迁移跑到一半失败"不会留下半成品 schema。
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()