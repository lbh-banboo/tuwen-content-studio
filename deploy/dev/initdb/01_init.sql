-- ############################################################################
-- WP05 / G0-18 数据库初始化脚本
--
-- 执行时机：PostgreSQL 容器「数据目录为空」时的首次启动，
--           由官方 entrypoint 自动按文件名顺序执行 /docker-entrypoint-initdb.d/*.sql。
--           已经在跑的库不会重复执行 —— 重置办法见 deploy/dev/README.md。
--
-- 设计口径（为什么只有这几张表）：
--   本脚本服务于 M0 冒烟（G0-18/G0-19/G0-21），只建「最小占位」，
--   不写全量业务表。全量模型在 M1 起用 Alembic 迁移管理（研发计划 §5 后端行）。
--   这里的表结构必须与 tools/m0/wp05_smoke/smoke.py 里的 bootstrap 保持一致，
--   否则「原生 PG 替代验证」与「容器内验证」会得出不同结论。
--
-- 引号口径：全文使用 ASCII 直引号。
-- ############################################################################

\set ON_ERROR_STOP on

-- ############################################################################
-- 1. 扩展
--    pg_trgm   : 模糊匹配（研发计划 §5 主库行明确要求）
--    pgcrypto  : gen_random_uuid() 等（PG13+ 内核已内置 gen_random_uuid，
--                保留 pgcrypto 是为了后续摘要/加密函数）
--    uuid-ossp : uuid_generate_v4() 等传统函数，兼容早期写法
--    vector    : pgvector，向量检索能力（ADR-15，单 profile <= 200 万 chunk 用 HNSW）
--
--    为什么 vector 单独用 DO/EXCEPTION 包起来：
--      pgvector 依赖镜像里预编译的 .so。若在「未装 pgvector 的 PG」上执行本脚本
--      （例如本机 brew 原生 PG），裸 CREATE EXTENSION 会让整个初始化中断、
--      后面的建表全部不执行，从而把「缺一个扩展」放大成「数据库不可用」。
--      这里降级为 WARNING 并继续，随后由冒烟脚本把「vector 是否可用」如实记入证据。
-- ############################################################################

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        CREATE EXTENSION IF NOT EXISTS vector;
        RAISE NOTICE '[WP05] vector 扩展已启用';
    ELSE
        RAISE WARNING '[WP05] 当前 PG 实例未提供 vector 扩展（缺少 pgvector 二进制），已跳过；向量检索能力需在内网 pgvector 镜像上复验';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING '[WP05] 创建 vector 扩展失败：% (SQLSTATE %)，已跳过', SQLERRM, SQLSTATE;
END
$$;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- 注意：扩展名含连字符，必须加双引号，否则 PG 会解析成减法表达式
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ############################################################################
-- 2. 最小 schema 占位
-- ############################################################################

-- smoke_meta：冒烟「写入 → 重启 → 校验」的元数据真相表。
--   为什么内容摘要（content_digest）要落库而不是只存文件：
--     重启后要判定「文件没丢且没被改」，必须有一个重启前就固化的期望值，
--     这个期望值放在数据库里，才能真正验证「PG 与对象存储两侧同时存活」。
CREATE TABLE IF NOT EXISTS smoke_meta (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid        NOT NULL,
    backend        text        NOT NULL,
    object_key     text        NOT NULL,
    content_digest text        NOT NULL,
    content_bytes  bigint      NOT NULL CHECK (content_bytes > 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    extra          jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- 冒烟脚本按 created_at 取「最近一次写入」，无索引时数据量大会退化为全表扫
CREATE INDEX IF NOT EXISTS smoke_meta_created_at_idx
    ON smoke_meta (created_at DESC);

COMMENT ON TABLE  smoke_meta IS 'WP05 环境冒烟元数据（M0 临时表，M1 由 Alembic 接管后可删）';
COMMENT ON COLUMN smoke_meta.content_digest IS '内容 SHA-256 十六进制；重启后校验对象未被篡改/丢失的期望值';

-- quota_accounts：配额账户的「最小形态」占位。
--   只保留 G0 阶段需要的字段（额度、已用、范围），不做计费/账本/审计，
--   真正的配额与账本模型在 M1 之后按文档 02 落地。
CREATE TABLE IF NOT EXISTS quota_accounts (
    account_key  text        PRIMARY KEY,
    scope        text        NOT NULL DEFAULT 'global',
    quota_bytes  bigint      NOT NULL DEFAULT 0 CHECK (quota_bytes >= 0),
    used_bytes   bigint      NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT quota_accounts_used_within_quota CHECK (used_bytes <= quota_bytes)
);

COMMENT ON TABLE quota_accounts IS '配额账户最小占位（M0）；全量配额/账本模型 M1 起落 Alembic 迁移';

-- ############################################################################
-- 3. 非超级用户应用角色
--    为什么必须建它：应用不得用超级用户连库（研发计划 §10 运维口径）。
--    这里建的是「示例角色」，只给到 public schema 的 DML 权限，
--    不给 DDL、不给 pg_read_server_files 等敏感权限。
--
--    口令口径（重要）：
--      initdb 脚本由 entrypoint 直接喂给 psql，读不到 compose 的环境变量，
--      因此这里只能写一个 dev 占位口令。
--      [待核实: .env.example 未提供 APP_DB_PASSWORD，M1 接入密钥管理后应改为
--       由 secret 注入并 ALTER ROLE 覆盖；本行必须在 M1 前替换]
-- ############################################################################

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rednote_app') THEN
        CREATE ROLE rednote_app LOGIN;
        RAISE NOTICE '[WP05] 已创建应用角色 rednote_app';
    ELSE
        RAISE NOTICE '[WP05] 应用角色 rednote_app 已存在，跳过创建';
    END IF;
END
$$;

ALTER ROLE rednote_app WITH
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD 'CHANGE_ME_app_role_dev_password';

-- public schema 在 PG15+ 已收回 PUBLIC 的 CREATE 权限，应用角色需显式授权
GRANT USAGE ON SCHEMA public TO rednote_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rednote_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rednote_app;

-- 后续由超级用户新建的表，同样自动带上应用角色权限，避免「新表忘了授权」的隐性故障
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rednote_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rednote_app;

-- ############################################################################
-- 4. 把扩展清单打到容器日志里，作为 G0-18 的可核对证据
-- ############################################################################

DO $$
DECLARE
    r record;
BEGIN
    RAISE NOTICE '[WP05] 初始化完成，扩展清单如下：';
    FOR r IN SELECT extname, extversion FROM pg_extension ORDER BY extname LOOP
        RAISE NOTICE '[WP05]   扩展 % = %', r.extname, r.extversion;
    END LOOP;
END
$$;
